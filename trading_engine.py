# ==============================================================
# [trading_engine.py] 무한매매 엔진 v5.0
#
# v5.0 핵심 수정 (구글 시트 로직 완전 동기화):
#   - 큰수(large_num) 개념 전면 적용
#     · 진입가 = 현재가 × (1 + 큰수%) → 호가단위 적용
#     · 수량 = 예산 ÷ 큰수  (보수적 계산)
#   - 전반전: price1 = min(별값, 큰수) / price2 = min(평단, 큰수)
#   - 후반전: price  = min(별값, 큰수)
#   - 줍줍:   큰수 미만인 가격만 (평단 기준 아님)
#   - 주문유형: 동시호가(LOC) / 지정가 / 시장가 명시
#   - config per-symbol 에 large_num_pct 추가 (기본 15%)
# ==============================================================
import asyncio
import datetime
import logging
import math
from zoneinfo import ZoneInfo

from kiwoom_api import KiwoomBroker, round_to_tick, get_tick_size
from database   import Database
from notifier   import Notifier

KST = ZoneInfo("Asia/Seoul")
log = logging.getLogger(__name__)


# ==============================================================
# 핵심 계산 함수 (순수 함수 — 테스트 가능)
# ==============================================================

def calc_large_num(cur_price: int, large_num_pct: float) -> int:
    """
    큰수 = 현재가 × (1 + 큰수%) → 호가단위 올림.
    동시호가 주문의 상한가 역할 — 실제 체결은 종가에 이루어짐.
    """
    return round_to_tick(int(cur_price * (1 + large_num_pct / 100.0)))


def calc_t_val(total_qty: int, one_portion_qty: int) -> float:
    """
    t_val = 누적 보유수량 / 1회차 기준 수량.

    원본 공식: t_val = 투자금액 / 총예산 × n  (금액 기반)
    우리 구현: 수량 기반 근사 — 호가 변동 시 오차 발생 가능
    정확도: 실제 매수 후 DB의 avg_price × total_qty로 역산 권장

    실전 영향:
    - 오차 <0.1T (호가 ±1% 범위) → 전반전/후반전 판단에 영향 미미
    - 리버스 진입 판단(T > split-1)에서만 정밀도 중요
    """
    if one_portion_qty <= 0:
        return 0.0
    return total_qty / one_portion_qty


def calc_t_val_by_amount(invested_amount: int, allocation: int, split: int) -> float:
    """
    T값 금액 기반 정확 계산 (원본 공식).
    DB에 avg_price * total_qty 저장 시 사용.
    """
    if allocation <= 0 or split <= 0:
        return 0.0
    return (invested_amount / allocation) * split


def calc_star_ratio(target_ratio: float, split: int, t_val: float) -> float:
    """
    별값 비율 = target_ratio - (target_ratio × 2/split × t_val)
    t_val이 늘수록 목표 수익률이 낮아짐 (손실을 줄이면서 탈출).
    """
    if split <= 0:
        return target_ratio
    ratio = target_ratio - (target_ratio * (2.0 / split) * t_val)
    return max(0.001, ratio)


def calc_star_price(avg_price: int, star_ratio: float) -> int:
    """별값가 = 평단 × (1 + star_ratio) → 호가단위 적용."""
    if avg_price <= 0:
        return 0
    return round_to_tick(int(avg_price * (1 + star_ratio)))


def calc_target_price(avg_price: int, target_ratio: float) -> int:
    """목표가 = 평단 × (1 + target_ratio) → 호가단위 적용."""
    if avg_price <= 0:
        return 0
    return round_to_tick(int(avg_price * (1 + target_ratio)))


def calc_one_portion_qty(one_portion_krw: int, ref_price: int) -> int:
    """1회차 기준 수량 = 예산 ÷ 참조가 (t_val 계산용)."""
    if ref_price <= 0:
        return 1
    return max(1, math.floor(one_portion_krw / ref_price))


# ==============================================================
# 무한매매 매수 계획 수립 (구글 시트 로직 완전 구현)
# ==============================================================

def plan_loc_buy(
    t_val: float,
    split: int,
    avg_price: int,
    star_price: int,
    one_portion_krw: int,
    cur_price: int,
    large_num: int = 0,        # 큰수 (0이면 star_price만 사용)
    target_price: int = 0,     # 목표가 (매도 계획용)
    total_qty: int = 0,        # 현재 보유수량 (매도 계획용)
) -> dict:
    """
    동시호가(LOC) 매수/매도 주문 계획 생성.

    ┌ 전반전 (t_val < split/2) ──────────────────────────────────
    │ price1 = min(별값 - 1호가,  큰수)   → 50% 예산
    │ price2 = min(평단가,        큰수)   → 나머지 예산
    │ q1 = floor(예산×0.5 / price1)
    │ q2 = max(0, floor(예산 / price2) - q1)
    │
    ├ 후반전 (t_val >= split/2) ────────────────────────────────
    │ price = min(별값 - 1호가, 큰수)
    │ qty   = floor(예산 / price)
    │
    └ 줍줍 (공통) ───────────────────────────────────────────────
      amt/(base+1) ~ amt/(base+5) 중 큰수 미만인 것만 (최대 5개)

    반환: {"buy": [...], "sell": [...]}
    """
    result = {"buy": [], "sell": []}

    if avg_price <= 0 or cur_price <= 0:
        return result

    # 큰수 기본값: star_price (없으면)
    if large_num <= 0:
        large_num = star_price if star_price > 0 else round_to_tick(int(cur_price * 1.15))

    half = split / 2.0
    is_first_half = (t_val < half)
    tick1 = get_tick_size(star_price) if star_price > 0 else 50

    # ── 매수용 별값 가격 (1호가 낮게 — 큰수 이하로 체결 유도)
    star_buy_price = max(1, star_price - tick1)
    star_buy_price = round_to_tick(star_buy_price)

    base_qty = 0

    if is_first_half:
        # ── 전반전 ───────────────────────────────────────────────
        price1 = min(star_buy_price, large_num)   # 별값% or 큰수
        price2 = min(avg_price,      large_num)   # 평단   or 큰수

        q1 = math.floor(one_portion_krw * 0.5 / price1) if price1 > 0 else 0
        q2 = max(0, math.floor(one_portion_krw / price2) - q1) if price2 > 0 else 0
        base_qty = q1 + q2

        if price1 == price2 == large_num:
            # 둘 다 큰수로 수렴
            result["buy"].append({
                "price": large_num, "qty": base_qty, "type": "동시호가",
                "desc": "⚓평단+별값(전량🔶큰수)"
            })
        else:
            if q1 > 0:
                lbl = "별값🔶큰수" if price1 == large_num else "별값⭐"
                result["buy"].append({
                    "price": price1, "qty": q1, "type": "동시호가",
                    "desc": f"💫{lbl}매수"
                })
            if q2 > 0:
                lbl = "평단🔶큰수" if price2 == large_num else "평단⚓"
                result["buy"].append({
                    "price": price2, "qty": q2, "type": "동시호가",
                    "desc": f"⚓{lbl}매수"
                })
    else:
        # ── 후반전 ───────────────────────────────────────────────
        price = min(star_buy_price, large_num)
        base_qty = math.floor(one_portion_krw / price) if price > 0 else 0
        if base_qty > 0:
            lbl = "🔶큰수" if price == large_num else "⭐별값"
            result["buy"].append({
                "price": price, "qty": base_qty, "type": "동시호가",
                "desc": f"💫{lbl}매수(후반전)"
            })

    # ── 줍줍 (대폭락 추가 매수): 큰수 미만인 가격만
    if base_qty > 0 and one_portion_krw > 0:
        tier_found = 0
        for i in range(1, 16):
            tier_qty   = base_qty + i
            tier_price = round_to_tick(int(one_portion_krw / tier_qty))
            if tier_price > 0 and tier_price < large_num:
                result["buy"].append({
                    "price": tier_price, "qty": 1, "type": "동시호가",
                    "desc": f"🧹줍줍({tier_found+1})"
                })
                tier_found += 1
            if tier_found >= 5:
                break

    # ── 매도 계획 (보유 중일 때)
    if total_qty > 0 and avg_price > 0:
        q_quarter = math.floor(total_qty * 0.25)
        q_remain  = total_qty - q_quarter

        if star_price > 0 and q_quarter > 0:
            result["sell"].append({
                "price": star_price, "qty": q_quarter, "type": "동시호가",
                "desc": "⭐별값매도(1/4쿼터)"
            })
        if target_price > 0 and q_remain > 0:
            result["sell"].append({
                "price": target_price, "qty": q_remain, "type": "지정가",
                "desc": "🎯목표가매도(잔여)"
            })

    return result


def plan_new_entry(one_portion_krw: int, large_num: int) -> dict:
    """
    qty=0 신규 진입 (새출발) 계획.
    ① 동시호가: 큰수 가격, 수량 = 예산 ÷ 큰수
    ② 줍줍 5개: 예산 ÷ (기준수량+1~5), 큰수 미만인 것만 → 구글 시트와 동일
    """
    result = {"buy": [], "sell": []}
    if large_num <= 0 or one_portion_krw <= 0:
        return result

    base_qty = math.floor(one_portion_krw / large_num)
    if base_qty > 0:
        result["buy"].append({
            "price": large_num, "qty": base_qty, "type": "동시호가",
            "desc": "🆕새출발매수(최초진입)"
        })

    # ── 줍줍: 예산/(기준수량+i), 큰수 미만만, 최대 5개 ─────────
    tier_found = 0
    for i in range(1, 16):
        tier_qty   = base_qty + i
        tier_price = round_to_tick(int(one_portion_krw / tier_qty))
        if tier_price > 0 and tier_price < large_num:
            result["buy"].append({
                "price": tier_price, "qty": 1, "type": "동시호가",
                "desc": f"🧹줍줍({tier_found+1})"
            })
            tier_found += 1
        if tier_found >= 5:
            break

    return result


# ==============================================================
# TradingEngine 클래스
# ==============================================================

class TradingEngine:
    """
    무한매매(INFINITE) + V-REV 전략 실행 엔진 v5.0
    """

    def __init__(self, broker: KiwoomBroker, db: Database,
                 notifier: Notifier, calendar=None):
        self.broker   = broker
        self.db       = db
        self.notifier = notifier
        self.calendar = calendar

    def _now(self):
        return datetime.datetime.now(KST)

    def _get_active_symbols(self, mode=None):
        symbols = self.broker.cfg.get("SYMBOLS", [])
        return [s for s in symbols
                if s.get("active", True)
                and (mode is None or s.get("mode", "INFINITE") == mode)]

    def _sym_params(self, sym: dict) -> dict:
        """종목 파라미터 통합 추출."""
        split        = sym.get("split_count", 10)
        target_ratio = sym.get("target_profit_pct", 5.0) / 100.0
        allocation   = sym.get("allocation_krw", 0)
        large_pct    = sym.get("large_num_pct", 15.0)      # ★ 큰수 기준 (기본 15%)
        one_portion  = allocation // split if split > 0 else allocation
        return {
            "split":        split,
            "target_ratio": target_ratio,
            "allocation":   allocation,
            "large_pct":    large_pct,
            "one_portion":  one_portion,
        }

    def _holdings_dict(self) -> dict:
        try:
            return {h["code"]: h for h in self.broker.get_holdings()}
        except Exception:
            return {}

    # ----------------------------------------------------------
    # ① 익절 감시 — 매 60초 (장중)
    # ----------------------------------------------------------
    async def check_and_sell_profit(self):
        for s in self._get_active_symbols("INFINITE"):
            try:
                await asyncio.to_thread(self._check_sell_one, s)
            except Exception as e:
                log.exception(f"[Engine] {s.get('code')} 익절감시 실패: {e}")

    def _check_sell_one(self, sym: dict):
        code = sym.get("code", "")
        name = sym.get("name", code)
        p    = self._sym_params(sym)
        # sym 참조 보관 (졸업 기록 시 사용)

        pos       = self.db.get_position(code)
        if not pos or pos.get("total_qty", 0) <= 0:
            return

        avg_price = int(pos.get("avg_price", 0))
        total_qty = int(pos.get("total_qty", 0))
        if avg_price <= 0:
            return

        cur = self.broker.get_current_price(code)
        if cur <= 0:
            return

        one_portion_qty = calc_one_portion_qty(p["one_portion"], avg_price)
        t_val       = calc_t_val(total_qty, one_portion_qty)
        star_ratio  = calc_star_ratio(p["target_ratio"], p["split"], t_val)
        star_price  = calc_star_price(avg_price, star_ratio)
        target_price= calc_target_price(avg_price, p["target_ratio"])

        log.info(
            f"[Engine] {name} | 현재:{cur:,}원 | 평단:{avg_price:,}원 | "
            f"별값:{star_price:,}원 | 목표:{target_price:,}원 | t_val:{t_val:.2f}"
        )

        # 별값 1/4 매도
        if cur >= star_price:
            sell_qty = math.ceil(total_qty / 4)
            if 0 < sell_qty <= total_qty:
                res    = self.broker.sell(code, sell_qty, price=star_price,
                                          order_type=self.broker.ORDER_LIMIT)
                profit = (star_price - avg_price) * sell_qty
                self.db.record_trade(
                    code=code, name=name, side="SELL",
                    qty=sell_qty, price=star_price,
                    order_no=res.get("order_no", ""),
                    profit=profit, profit_pct=star_ratio * 100,
                )
                self._update_pos_after_sell(code, name, pos, sell_qty, avg_price)
                self.notifier.notify_sell(
                    code, name, sell_qty, star_price, sell_qty * star_price,
                    profit, star_ratio * 100
                )
                log.info(f"[Engine] {name} 별값 1/4 매도: {sell_qty}주 @{star_price:,}원")
                return

        # 목표가 전량 매도
        if cur >= target_price:
            res    = self.broker.sell(code, total_qty, price=0,
                                      order_type=self.broker.ORDER_MARKET)
            profit = (cur - avg_price) * total_qty
            self.db.record_trade(
                code=code, name=name, side="SELL",
                qty=total_qty, price=cur,
                order_no=res.get("order_no", ""),
                profit=profit, profit_pct=p["target_ratio"] * 100,
            )
            round_no = pos.get("round_no", 1)
            self.db.upsert_position(
                code=code, name=name, mode="INFINITE",
                round_no=round_no + 1,
                avg_price=0, total_qty=0,
            )
            # 졸업 기록
            allocation = sym.get("allocation_krw", 0) if sym else 0
            self.db.record_cycle_graduation(
                code=code, name=name, round_no=round_no,
                principal=allocation,
                final_amount=allocation + profit,
                final_t_val=t_val,
                exit_type="목표가달성",
            )
            self.notifier.notify_infinite_sell(
                code=code, name=name,
                qty=total_qty, price=cur,
                amount=total_qty * cur,
                profit=profit,
                profit_pct=p["target_ratio"] * 100,
                sell_type="목표가매도(전량)",
                reason=f"목표가 +{p['target_ratio']*100:.1f}% 도달 🎉 {round_no}회차 졸업!",
                remain_qty=0,
                avg_price=avg_price,
            )
            log.info(f"[Engine] {name} {round_no}회차 졸업! @{cur:,}원 수익 {profit:+,}원 ✅")

    def _update_pos_after_sell(self, code, name, pos, sold_qty, avg_price):
        remain = int(pos.get("total_qty", 0)) - sold_qty
        if remain <= 0:
            self.db.upsert_position(
                code=code, name=name, mode="INFINITE",
                round_no=pos.get("round_no", 1) + 1,
                avg_price=0, total_qty=0,
            )
        else:
            self.db.upsert_position(
                code=code, name=name, mode="INFINITE",
                round_no=pos.get("round_no", 1),
                avg_price=avg_price, total_qty=remain,
            )

    # ----------------------------------------------------------
    # ② 동시호가(LOC) 매수 — 15:10 (scheduler_trade 호출)
    # ----------------------------------------------------------
    async def loc_buy(self):
        for s in self._get_active_symbols("INFINITE"):
            try:
                await asyncio.to_thread(self._loc_buy_one, s)
            except Exception as e:
                log.exception(f"[Engine] {s.get('code')} LOC 매수 실패: {e}")

    def _loc_buy_one(self, sym: dict):
        code = sym.get("code", "")
        name = sym.get("name", code)
        p    = self._sym_params(sym)

        pos       = self.db.get_position(code) or {}
        avg_price = int(pos.get("avg_price", 0))
        total_qty = int(pos.get("total_qty", 0))

        cur = self.broker.get_current_price(code)
        if cur <= 0:
            log.warning(f"[Engine] {name} 현재가 조회 실패 — LOC 스킵")
            return

        # ── 큰수 계산 ─────────────────────────────────────────
        large_num = calc_large_num(cur, p["large_pct"])

        # ── 신규 진입 (qty=0) ────────────────────────────────
        if total_qty == 0 or avg_price == 0:
            plan = plan_new_entry(p["one_portion"], large_num)
            for o in plan["buy"]:
                res = self.broker.buy(code, o["qty"], price=o["price"],
                                      order_type=self.broker.ORDER_LIMIT)
                self._record_buy(code, name, pos, o["qty"], o["price"], o["desc"])
                log.info(f"[Engine] {name} {o['desc']}: {o['qty']}주 @{o['price']:,}원 (큰수:{large_num:,})")
            return

        # ── t_val 계산 & 리버스 판단 ─────────────────────────
        one_portion_qty = calc_one_portion_qty(p["one_portion"], avg_price)
        t_val      = calc_t_val(total_qty, one_portion_qty)
        star_ratio = calc_star_ratio(p["target_ratio"], p["split"], t_val)
        star_price = calc_star_price(avg_price, star_ratio)

        is_reverse = t_val > (p["split"] - 1)
        if is_reverse:
            self._loc_reverse_buy(sym, pos, t_val, avg_price, star_price,
                                  p["one_portion"], cur, large_num)
            return

        # ── 정상 매수 계획 ────────────────────────────────────
        target_price = calc_target_price(avg_price, p["target_ratio"])
        plan = plan_loc_buy(
            t_val=t_val, split=p["split"],
            avg_price=avg_price, star_price=star_price,
            one_portion_krw=p["one_portion"], cur_price=cur,
            large_num=large_num,
            target_price=target_price, total_qty=total_qty,
        )

        for o in plan["buy"]:
            if o["qty"] <= 0:
                continue
            res = self.broker.buy(code, o["qty"], price=o["price"],
                                  order_type=self.broker.ORDER_LIMIT)
            self._record_buy(code, name, pos, o["qty"], o["price"], o["desc"])
            log.info(
                f"[Engine] {name} {o['desc']}: {o['qty']}주 @{o['price']:,}원"
            )
            pos = self.db.get_position(code) or pos

    def _loc_reverse_buy(self, sym, pos, t_val, avg_price,
                          star_price, one_portion_krw, cur, large_num):
        """리버스 모드: 5MA(별값) 기준 매수 + 소량 매도."""
        code    = sym.get("code", "")
        name    = sym.get("name", code)
        qty     = int(pos.get("total_qty", 0))
        rev_day = pos.get("reverse_day", 1)
        split   = sym.get("split_count", 10)

        log.warning(f"[Engine] {name} 리버스 모드 {rev_day}일차 (t_val={t_val:.2f})")

        if rev_day == 1:
            # 1일차: 시장가 10% 강제 매도
            sell_qty = max(1, math.floor(qty / 10))
            self.broker.sell(code, sell_qty, order_type=self.broker.ORDER_MARKET)
            self.db.record_trade(code=code, name=name, side="SELL",
                                 qty=sell_qty, price=cur, profit=0, profit_pct=0)
            self.notifier.send(
                f"🚨 <b>{name} 리버스 1일차</b>\n"
                f"시장가 10% 매도: {sell_qty:,}주"
            )
            self.db.upsert_position(
                code=code, name=name, mode="INFINITE",
                round_no=pos.get("round_no", 1),
                avg_price=avg_price, total_qty=qty - sell_qty,
                reverse_day=2,
            )
        else:
            # 2일차+: 별값 기준 매수 + 소량 매도
            p_buy = max(1, star_price - get_tick_size(star_price))
            p_buy = min(p_buy, large_num)                    # 큰수 초과 금지
            q_buy = math.floor(one_portion_krw / p_buy) if p_buy > 0 else 0
            if q_buy > 0:
                self.broker.buy(code, q_buy, price=p_buy,
                                order_type=self.broker.ORDER_LIMIT)
                self._record_buy(code, name, pos, q_buy, p_buy, "⚓리버스매수")

            sell_div = 10 if split <= 20 else 20
            sell_qty = max(1, math.floor(qty / sell_div))
            self.broker.sell(code, sell_qty, price=star_price,
                             order_type=self.broker.ORDER_LIMIT)
            log.info(f"[Engine] {name} 리버스 별값매도: {sell_qty}주 @{star_price:,}원")

    def _record_buy(self, code, name, pos, qty, price, desc):
        pos     = pos or {}
        old_qty = int(pos.get("total_qty", 0))
        old_avg = int(pos.get("avg_price", 0))
        new_qty = old_qty + qty
        new_avg = (old_avg * old_qty + price * qty) // new_qty if new_qty > 0 else price
        self.db.upsert_position(
            code=code, name=name, mode="INFINITE",
            round_no=pos.get("round_no", 1),
            avg_price=new_avg, total_qty=new_qty,
        )
        self.db.record_trade(code=code, name=name, side="BUY", qty=qty, price=price)
        self.notifier.notify_infinite_buy(
            code=code, name=name,
            qty=qty, price=price, amount=qty * price,
            desc=desc,
            avg_price=new_avg, total_qty=new_qty,
        )

    # ----------------------------------------------------------
    # ③ 동시호가 재주문 — 15:20
    # ----------------------------------------------------------
    async def auction_buy(self):
        today = self._now().strftime("%Y-%m-%d")
        for s in self._get_active_symbols("INFINITE"):
            code = s.get("code", "")
            try:
                buys = [t for t in self.db.get_trades_by_date(today)
                        if t.get("code") == code and t.get("side") == "BUY"]
                if buys:
                    continue
                p   = self._sym_params(s)
                cur = self.broker.get_current_price(code)
                if cur <= 0:
                    continue
                large_num = calc_large_num(cur, p["large_pct"])
                qty = math.floor(p["one_portion"] / large_num) if large_num > 0 else 0
                if qty > 0:
                    self.broker.buy(code, qty, price=0,
                                    order_type=self.broker.ORDER_AUCTION)
                    pos = self.db.get_position(code)
                    self._record_buy(code, s.get("name", code), pos, qty, cur, "🔔동시호가재주문")
            except Exception as e:
                log.exception(f"[Engine] {code} 동시호가 재주문 실패: {e}")

    # ----------------------------------------------------------
    # ④ V-REV 리밸런싱 — 09:10
    # ----------------------------------------------------------
    async def vrev_rebalance(self, symbols: list):
        for s in symbols:
            try:
                await asyncio.to_thread(self._vrev_one, s)
            except Exception as e:
                log.exception(f"[Engine] {s.get('code')} V-REV 실패: {e}")

    def _vrev_one(self, sym: dict):
        code     = sym.get("code", "")
        name     = sym.get("name", code)
        band_pct = sym.get("vrev_band_pct", 3.0) / 100.0
        target_v = sym.get("allocation_krw", 0)
        p        = self._sym_params(sym)

        vrev_st = self.db.get_vrev_state(code)
        if vrev_st and vrev_st.get("target_value", 0) > 0:
            target_v = int(vrev_st["target_value"])

        today = self._now().strftime("%Y-%m-%d")
        if vrev_st and vrev_st.get("last_rebal_date") == today:
            return

        holdings  = self._holdings_dict()
        h         = holdings.get(code, {})
        qty       = int(h.get("qty", 0))
        avg_price = int(h.get("avg_price", 0))
        cur       = self.broker.get_current_price(code)
        if cur <= 0 or target_v <= 0:
            return

        current_v = qty * cur
        deviation = (current_v - target_v) / target_v

        daily_limit = sym.get("daily_buy_limit_krw",
                              p["allocation"] // p["split"])

        if deviation < -band_pct:
            gap        = target_v - current_v
            buy_budget = min(gap, daily_limit)
            large_num  = calc_large_num(cur, p["large_pct"])

            buy1_price = round_to_tick(cur)
            buy1_qty   = math.floor(buy_budget * 0.6 / buy1_price) if buy1_price > 0 else 0
            if buy1_qty > 0:
                self.broker.buy(code, buy1_qty, price=buy1_price,
                                order_type=self.broker.ORDER_LIMIT)
                self.db.record_trade(code=code, name=name, side="BUY",
                                     qty=buy1_qty, price=buy1_price)

            buy2_price = round_to_tick(int(cur * 0.995))
            buy2_qty   = math.floor(buy_budget * 0.4 / buy2_price) if buy2_price > 0 else 0
            if buy2_qty > 0:
                self.broker.buy(code, buy2_qty, price=buy2_price,
                                order_type=self.broker.ORDER_LIMIT)
                self.db.record_trade(code=code, name=name, side="BUY",
                                     qty=buy2_qty, price=buy2_price)

            self.db.upsert_vrev_state(code, target_v, today)
            self.notifier.send(
                f"🔄 <b>V-REV 매수</b> {name}\n"
                f"편차: {deviation*100:+.1f}% | "
                f"Buy1: {buy1_qty}주@{buy1_price:,}원 / "
                f"Buy2: {buy2_qty}주@{buy2_price:,}원"
            )

        elif deviation > band_pct:
            gap        = current_v - target_v
            pop1_price = round_to_tick(int(avg_price * 1.006))
            pop1_qty   = math.floor(gap * 0.5 / pop1_price) if pop1_price > 0 else 0
            pop1_qty   = min(pop1_qty, qty)
            if pop1_qty > 0:
                self.broker.sell(code, pop1_qty, price=pop1_price,
                                 order_type=self.broker.ORDER_LIMIT)
                profit = (pop1_price - avg_price) * pop1_qty
                self.db.record_trade(code=code, name=name, side="SELL",
                                     qty=pop1_qty, price=pop1_price,
                                     profit=profit, profit_pct=0.6)

            pop2_price = round_to_tick(int(avg_price * 1.005))
            pop2_qty   = min(math.floor(gap * 0.5 / pop2_price), qty - pop1_qty)
            if pop2_qty > 0:
                self.broker.sell(code, pop2_qty, price=pop2_price,
                                 order_type=self.broker.ORDER_LIMIT)
                profit = (pop2_price - avg_price) * pop2_qty
                self.db.record_trade(code=code, name=name, side="SELL",
                                     qty=pop2_qty, price=pop2_price,
                                     profit=profit, profit_pct=0.5)

            self.db.upsert_vrev_state(code, target_v, today)
            self.notifier.send(
                f"🔄 <b>V-REV 매도</b> {name}\n"
                f"편차: {deviation*100:+.1f}% | "
                f"Pop1: {pop1_qty}주@{pop1_price:,}원 / "
                f"Pop2: {pop2_qty}주@{pop2_price:,}원"
            )
        else:
            self.db.upsert_vrev_state(code, target_v, today)

    # ----------------------------------------------------------
    # ⑤ 유틸
    # ----------------------------------------------------------
    def morning_cash_check(self):
        symbols  = self._get_active_symbols()
        required = sum(
            s.get("daily_buy_limit_krw",
                  s.get("allocation_krw", 0) // s.get("split_count", 10))
            for s in symbols
        )
        try:
            balance = self.broker.get_balance()
            cash    = balance.get("withdrawable", 0)
        except Exception:
            cash = 0
        ok = cash >= required
        if not ok:
            self.notifier.notify_low_balance(cash, required)
        return ok, cash, required

    def generate_daily_report(self):
        today  = self._now().strftime("%Y-%m-%d")
        trades = self.db.get_trades_by_date(today)
        result = {}
        for s in self._get_active_symbols():
            code = s.get("code", "")
            name = s.get("name", code)
            st   = [t for t in trades if t.get("code") == code]
            buy  = sum(t.get("amount", 0) for t in st if t.get("side") == "BUY")
            sell = sum(t.get("amount", 0) for t in st if t.get("side") == "SELL")
            result[code] = {"name": name, "buy": buy, "sell": sell, "pnl": sell - buy}
        return result


    # ----------------------------------------------------------
    # ⑥ /sync 주문계획 생성 (telegram_bot.py 에서 호출)
    # ----------------------------------------------------------
    def build_sync_plan(self, sym: dict, cur: int, pos: dict) -> dict:
        """
        /sync 화면용 주문 계획 딕셔너리 반환.
        telegram_bot._send_sync_report() 에서 사용.
        """
        p         = self._sym_params(sym)
        avg_price = int(pos.get("avg_price", 0)) if pos else 0
        total_qty = int(pos.get("total_qty", 0)) if pos else 0
        large_num = calc_large_num(cur, p["large_pct"]) if cur > 0 else 0

        if total_qty == 0 or avg_price == 0:
            plan = plan_new_entry(p["one_portion"], large_num)

            # ── 진입 후 예상 매도 계획 ─────────────────────────
            # 예상 체결가 ≈ 현재가 (동시호가 종가 체결)
            # t_val = 1.0 (1회차 진입 직후), 예상 보유수량 = 기준수량
            if cur > 0 and large_num > 0:
                est_avg      = cur
                est_qty      = math.floor(p["one_portion"] / large_num)
                t_val_after  = 1.0
                sr_after     = calc_star_ratio(p["target_ratio"], p["split"], t_val_after)
                est_star     = calc_star_price(est_avg, sr_after)
                est_target   = calc_target_price(est_avg, p["target_ratio"])
                q_quarter    = max(1, math.floor(est_qty * 0.25))
                q_remain     = max(0, est_qty - q_quarter)

                if est_star > 0 and q_quarter > 0:
                    plan["sell"].append({
                        "price": est_star, "qty": q_quarter, "type": "동시호가",
                        "desc": "⭐별값매도(1/4쿼터) [진입후 예상]"
                    })
                if est_target > 0 and q_remain > 0:
                    plan["sell"].append({
                        "price": est_target, "qty": q_remain, "type": "지정가",
                        "desc": "🎯목표가매도(잔여) [진입후 예상]"
                    })

                return {
                    "phase":        "✨새출발",
                    "t_val":        0.0,
                    "star_ratio":   sr_after,
                    "star_price":   est_star,
                    "target_price": est_target,
                    "large_num":    large_num,
                    "plan":         plan,
                }

            return {
                "phase":       "✨새출발",
                "t_val":       0.0,
                "star_ratio":  p["target_ratio"],
                "star_price":  0,
                "target_price":0,
                "large_num":   large_num,
                "plan":        plan,
            }

        one_portion_qty = calc_one_portion_qty(p["one_portion"], avg_price)
        t_val       = calc_t_val(total_qty, one_portion_qty)
        star_ratio  = calc_star_ratio(p["target_ratio"], p["split"], t_val)
        star_price  = calc_star_price(avg_price, star_ratio)
        target_price= calc_target_price(avg_price, p["target_ratio"])
        is_rev      = t_val > (p["split"] - 1)
        half        = p["split"] / 2.0

        if is_rev:
            phase = "🚨리버스"
        elif t_val < half:
            phase = "🌓전반전"
        else:
            phase = "🌕후반전"

        plan = plan_loc_buy(
            t_val=t_val, split=p["split"],
            avg_price=avg_price, star_price=star_price,
            one_portion_krw=p["one_portion"], cur_price=cur,
            large_num=large_num,
            target_price=target_price, total_qty=total_qty,
        )
        return {
            "phase":       phase,
            "t_val":       t_val,
            "star_ratio":  star_ratio,
            "star_price":  star_price,
            "target_price":target_price,
            "large_num":   large_num,
            "plan":        plan,
        }
