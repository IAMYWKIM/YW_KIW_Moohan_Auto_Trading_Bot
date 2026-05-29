# ==============================================================
# [trading_engine.py] 무한매매 엔진 v6.0
#
# 승승장군 issues #59~#66 완전 재분석 반영
#
# v6.0 핵심 변경 (원본과의 GAP 완전 해소):
#
#   [무한매매 LOC]
#   - 기존과 동일: 15:10 동시호가 큰수+줍줍 ✅
#
#   [V-REV 완전 재구현]
#   - 기존: 09:10 목표밸류 편차 기반 (오류)
#   - 수정: 15:10 동시호가 + SMA5 기준 역추세
#     매수: 현재가 < SMA5 → 예산의 1/n 동시호가 매수
#     매도: 현재가 > SMA5 → 평단×1.006 지정가 매도
#   - 무한매매와 동일 시간(15:10)에 함께 실행
#   - 일한도(daily_buy_limit_krw) 안전장치 유지
#
#   [AVWAP]
#   - 진입조건: vwap_5m_avg > intraday_vwap ✅ (v2.0 수정 유지)
#   - 최소목표: 2.03% (국내 왕복수수료 0.03% 반영) ✅
#   - 타임라인: 09:30~14:30 교전 ✅
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
# 핵심 계산 함수
# ==============================================================

def calc_large_num(cur_price: int, large_num_pct: float) -> int:
    return round_to_tick(int(cur_price * (1 + large_num_pct / 100.0)))


def calc_t_val(total_qty: int, one_portion_qty: int) -> float:
    if one_portion_qty <= 0:
        return 0.0
    return total_qty / one_portion_qty


def calc_t_val_by_amount(invested_amount: int, allocation: int, split: int) -> float:
    if allocation <= 0 or split <= 0:
        return 0.0
    return (invested_amount / allocation) * split


def calc_star_ratio(target_ratio: float, split: int, t_val: float) -> float:
    if split <= 0:
        return target_ratio
    ratio = target_ratio - (target_ratio * (2.0 / split) * t_val)
    return max(0.001, ratio)


def calc_star_price(avg_price: int, star_ratio: float) -> int:
    if avg_price <= 0:
        return 0
    return round_to_tick(int(avg_price * (1 + star_ratio)))


def calc_target_price(avg_price: int, target_ratio: float) -> int:
    if avg_price <= 0:
        return 0
    return round_to_tick(int(avg_price * (1 + target_ratio)))


def calc_one_portion_qty(one_portion_krw: int, ref_price: int) -> int:
    if ref_price <= 0:
        return 1
    return max(1, math.floor(one_portion_krw / ref_price))


def plan_loc_buy(
    t_val: float, split: int,
    avg_price: int, star_price: int,
    one_portion_krw: int, cur_price: int,
    large_num: int = 0,
    target_price: int = 0,
    total_qty: int = 0,
) -> dict:
    result = {"buy": [], "sell": []}
    if avg_price <= 0 or cur_price <= 0:
        return result
    if large_num <= 0:
        large_num = star_price if star_price > 0 else round_to_tick(int(cur_price * 1.15))

    half       = split / 2.0
    is_first_half = (t_val < half)
    tick1      = get_tick_size(star_price) if star_price > 0 else 50
    star_buy_price = max(1, round_to_tick(star_price - tick1))
    base_qty   = 0

    if is_first_half:
        price1 = min(star_buy_price, large_num)
        price2 = min(avg_price,      large_num)
        q1 = math.floor(one_portion_krw * 0.5 / price1) if price1 > 0 else 0
        q2 = max(0, math.floor(one_portion_krw / price2) - q1) if price2 > 0 else 0
        base_qty = q1 + q2
        if price1 == price2 == large_num:
            result["buy"].append({"price": large_num, "qty": base_qty,
                                   "type": "동시호가", "desc": "전량🔶큰수"})
        else:
            if q1 > 0:
                lbl = "별값🔶큰수" if price1 == large_num else "별값⭐"
                result["buy"].append({"price": price1, "qty": q1,
                                       "type": "동시호가", "desc": f"💫{lbl}매수"})
            if q2 > 0:
                lbl = "평단🔶큰수" if price2 == large_num else "평단⚓"
                result["buy"].append({"price": price2, "qty": q2,
                                       "type": "동시호가", "desc": f"⚓{lbl}매수"})
    else:
        price  = min(star_buy_price, large_num)
        base_qty = math.floor(one_portion_krw / price) if price > 0 else 0
        if base_qty > 0:
            lbl = "🔶큰수" if price == large_num else "⭐별값"
            result["buy"].append({"price": price, "qty": base_qty,
                                   "type": "동시호가", "desc": f"💫{lbl}매수(후반전)"})

    # 줍줍
    if base_qty > 0 and one_portion_krw > 0:
        tier_found = 0
        for i in range(1, 16):
            tier_qty   = base_qty + i
            tier_price = round_to_tick(int(one_portion_krw / tier_qty))
            if tier_price > 0 and tier_price < large_num:
                result["buy"].append({"price": tier_price, "qty": 1,
                                       "type": "동시호가", "desc": f"🧹줍줍({tier_found+1})"})
                tier_found += 1
            if tier_found >= 5:
                break

    # 매도 계획
    if total_qty > 0 and avg_price > 0:
        q_quarter = math.floor(total_qty * 0.25)
        q_remain  = max(0, total_qty - q_quarter)
        if star_price > 0 and q_quarter > 0:
            result["sell"].append({"price": star_price, "qty": q_quarter,
                                    "type": "동시호가", "desc": "⭐별값매도(1/4쿼터)"})
        if target_price > 0 and q_remain > 0:
            result["sell"].append({"price": target_price, "qty": q_remain,
                                    "type": "지정가", "desc": "🎯목표가매도(잔여)"})
    return result


def plan_new_entry(one_portion_krw: int, large_num: int) -> dict:
    result = {"buy": [], "sell": []}
    if large_num <= 0 or one_portion_krw <= 0:
        return result
    base_qty = math.floor(one_portion_krw / large_num)
    if base_qty > 0:
        result["buy"].append({"price": large_num, "qty": base_qty,
                               "type": "동시호가", "desc": "🆕새출발매수(최초진입)"})
    tier_found = 0
    for i in range(1, 16):
        tier_qty   = base_qty + i
        tier_price = round_to_tick(int(one_portion_krw / tier_qty))
        if tier_price > 0 and tier_price < large_num:
            result["buy"].append({"price": tier_price, "qty": 1,
                                   "type": "동시호가", "desc": f"🧹줍줍({tier_found+1})"})
            tier_found += 1
        if tier_found >= 5:
            break
    return result


# ==============================================================
# TradingEngine
# ==============================================================
class TradingEngine:

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
        split        = sym.get("split_count", 10)
        target_ratio = sym.get("target_profit_pct", 5.0) / 100.0
        allocation   = sym.get("allocation_krw", 0)
        large_pct    = sym.get("large_num_pct", 15.0)
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
    # ① 익절 감시 — 매 60초
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

        pos = self.db.get_position(code)
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
        t_val        = calc_t_val(total_qty, one_portion_qty)
        star_ratio   = calc_star_ratio(p["target_ratio"], p["split"], t_val)
        star_price   = calc_star_price(avg_price, star_ratio)
        target_price = calc_target_price(avg_price, p["target_ratio"])

        # 별값 1/4 매도
        if cur >= star_price and star_price > 0:
            sell_qty = max(1, math.ceil(total_qty / 4))
            if 0 < sell_qty <= total_qty:
                res    = self.broker.sell(code, sell_qty, price=star_price,
                                          order_type=self.broker.ORDER_LIMIT)
                profit = (star_price - avg_price) * sell_qty
                self.db.record_trade(code=code, name=name, side="SELL",
                                     qty=sell_qty, price=star_price,
                                     profit=profit, profit_pct=star_ratio*100)
                self._update_pos_after_sell(code, name, pos, sell_qty, avg_price)
                self.notifier.notify_infinite_sell(
                    code=code, name=name,
                    qty=sell_qty, price=star_price,
                    amount=sell_qty * star_price,
                    profit=profit, profit_pct=star_ratio*100,
                    sell_type="별값매도(1/4)",
                    reason=f"별값 {star_ratio*100:.1f}% 도달",
                    remain_qty=total_qty - sell_qty,
                    avg_price=avg_price,
                )
                log.info(f"[Engine] {name} 별값 1/4 매도: {sell_qty}주 @{star_price:,}원")
                return

        # 목표가 전량 매도
        if cur >= target_price and target_price > 0:
            res    = self.broker.sell(code, total_qty, price=0,
                                      order_type=self.broker.ORDER_MARKET)
            profit = (cur - avg_price) * total_qty
            round_no = pos.get("round_no", 1)
            self.db.upsert_position(code=code, name=name, mode="INFINITE",
                                    round_no=round_no + 1,
                                    avg_price=0, total_qty=0)
            self.db.record_cycle_graduation(
                code=code, name=name, round_no=round_no,
                principal=p["allocation"],
                final_amount=p["allocation"] + profit,
                final_t_val=t_val, exit_type="목표가달성",
            )
            self.notifier.notify_infinite_sell(
                code=code, name=name,
                qty=total_qty, price=cur,
                amount=total_qty * cur,
                profit=profit, profit_pct=p["target_ratio"]*100,
                sell_type="목표가매도(전량)",
                reason=f"목표가 +{p['target_ratio']*100:.1f}% 도달 🎉 {round_no}회차 졸업!",
                remain_qty=0, avg_price=avg_price,
            )
            log.info(f"[Engine] {name} {round_no}회차 졸업! @{cur:,}원 +{profit:,}원")

    def _update_pos_after_sell(self, code, name, pos, sold_qty, avg_price):
        remain = int(pos.get("total_qty", 0)) - sold_qty
        if remain <= 0:
            self.db.upsert_position(code=code, name=name, mode="INFINITE",
                                    round_no=pos.get("round_no", 1) + 1,
                                    avg_price=0, total_qty=0)
        else:
            self.db.upsert_position(code=code, name=name, mode="INFINITE",
                                    round_no=pos.get("round_no", 1),
                                    avg_price=avg_price, total_qty=remain)

    # ----------------------------------------------------------
    # ② LOC 매수 — 15:10 (무한매매 + V-REV 통합)
    # ----------------------------------------------------------
    async def loc_buy(self):
        """무한매매(INFINITE) 종목 15:10 동시호가 매수."""
        for s in self._get_active_symbols("INFINITE"):
            try:
                await asyncio.to_thread(self._loc_buy_one, s)
            except Exception as e:
                log.exception(f"[Engine] {s.get('code')} LOC 매수 실패: {e}")

    async def vrev_loc(self):
        """
        V-REV 15:10 동시호가 매수/매도 (승승장군 원본 알고리즘)

        원본 원칙 (issue #59 #61):
          매수: 현재가 < SMA5 (5일 이평) → 예산 1/n 동시호가 매수
          매도: 현재가 > SMA5            → 평단×1.006 동시호가 매도
          안전: 일한도(daily_buy_limit_krw) 준수
        """
        for s in self._get_active_symbols("VREV"):
            try:
                await asyncio.to_thread(self._vrev_loc_one, s)
            except Exception as e:
                log.exception(f"[Engine] {s.get('code')} V-REV LOC 실패: {e}")

    def _vrev_loc_one(self, sym: dict):
        code  = sym.get("code", "")
        name  = sym.get("name", code)
        p     = self._sym_params(sym)
        limit = sym.get("daily_buy_limit_krw", p["one_portion"])

        cur  = self.broker.get_current_price(code)
        sma5 = self._get_sma5(code)

        if cur <= 0 or sma5 <= 0:
            log.warning(f"[Engine] V-REV {name} 현재가/SMA5 조회 실패 — 스킵")
            return

        pos       = self.db.get_position(code) or {}
        avg_price = int(pos.get("avg_price", 0))
        total_qty = int(pos.get("total_qty", 0))

        pct_diff = (cur - sma5) / sma5 * 100

        log.info(
            f"[Engine] V-REV {name} | "
            f"현재:{cur:,} SMA5:{sma5:,} ({pct_diff:+.2f}%) | "
            f"보유:{total_qty}주 평단:{avg_price:,}"
        )

        if cur < sma5:
            # ── 매수: 현재가 < SMA5 ────────────────────────────
            buy_budget = min(limit, p["one_portion"])
            buy_price  = round_to_tick(cur)
            qty        = math.floor(buy_budget / buy_price) if buy_price > 0 else 0
            if qty <= 0:
                log.info(f"[Engine] V-REV {name} 매수 수량 0 — 스킵")
                return

            res    = self.broker.buy(code, qty, price=buy_price,
                                     order_type=self.broker.ORDER_AUCTION)
            new_qty = total_qty + qty
            new_avg = ((avg_price * total_qty + buy_price * qty) // new_qty
                       if new_qty > 0 else buy_price)
            self.db.upsert_position(code=code, name=name, mode="VREV",
                                    round_no=pos.get("round_no", 1),
                                    avg_price=new_avg, total_qty=new_qty)
            self.db.record_trade(code=code, name=name, side="BUY",
                                 qty=qty, price=buy_price)
            self.notifier.notify_infinite_buy(
                code=code, name=name,
                qty=qty, price=buy_price, amount=qty * buy_price,
                order_type="동시호가",
                desc=f"⚖️V-REV 매수 (현재가 {pct_diff:.1f}% < SMA5)",
                avg_price=new_avg, total_qty=new_qty,
            )
            log.info(f"[Engine] V-REV {name} 동시호가 매수 {qty}주 @{buy_price:,}원")

        elif cur > sma5 and total_qty > 0 and avg_price > 0:
            # ── 매도: 현재가 > SMA5 ────────────────────────────
            # 승승장군 원본: 평단×1.006 지정가 매도 (팝 레이어)
            pop_price = round_to_tick(int(avg_price * 1.006))
            sell_qty  = max(1, math.floor(total_qty / (p["split"] // 2 or 1)))
            sell_qty  = min(sell_qty, total_qty)

            if pop_price <= cur:
                # 이미 팝 가격 넘었으면 시장가
                res    = self.broker.sell(code, sell_qty, price=0,
                                          order_type=self.broker.ORDER_MARKET)
            else:
                res    = self.broker.sell(code, sell_qty, price=pop_price,
                                          order_type=self.broker.ORDER_LIMIT)

            profit = (pop_price - avg_price) * sell_qty
            remain = total_qty - sell_qty
            self.db.upsert_position(code=code, name=name, mode="VREV",
                                    round_no=pos.get("round_no", 1),
                                    avg_price=avg_price if remain > 0 else 0,
                                    total_qty=remain)
            self.db.record_trade(code=code, name=name, side="SELL",
                                 qty=sell_qty, price=pop_price, profit=profit)
            self.notifier.notify_infinite_sell(
                code=code, name=name,
                qty=sell_qty, price=pop_price, amount=sell_qty * pop_price,
                profit=profit, profit_pct=0.6,
                sell_type="V-REV 팝(Pop)",
                reason=f"현재가 {pct_diff:.1f}% > SMA5",
                remain_qty=remain, avg_price=avg_price,
            )
            log.info(f"[Engine] V-REV {name} 팝 매도 {sell_qty}주 @{pop_price:,}원")
        else:
            log.info(f"[Engine] V-REV {name} 교전 없음 (SMA5 범위 내)")

    def _get_sma5(self, code: str) -> int:
        """5일 이동평균가 조회. API 지원 시 실제 데이터, 없으면 추정."""
        try:
            info = self.broker.get_stock_info(code)
            sma5 = info.get("sma5", 0)
            if sma5 > 0:
                return int(sma5)
            # fallback: 전일종가 사용 (임시)
            prev = info.get("prev_close", 0)
            return int(prev) if prev > 0 else 0
        except Exception as e:
            log.warning(f"[Engine] SMA5 조회 실패 {code}: {e}")
            return 0

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

        large_num = calc_large_num(cur, p["large_pct"])

        if total_qty == 0 or avg_price == 0:
            plan = plan_new_entry(p["one_portion"], large_num)
            # 진입 후 예상 매도 계획
            if cur > 0 and large_num > 0:
                est_qty  = math.floor(p["one_portion"] / large_num)
                t_after  = 1.0
                sr_after = calc_star_ratio(p["target_ratio"], p["split"], t_after)
                est_star = calc_star_price(cur, sr_after)
                est_tgt  = calc_target_price(cur, p["target_ratio"])
                q_q = max(1, math.floor(est_qty * 0.25))
                q_r = max(0, est_qty - q_q)
                if est_star > 0 and q_q > 0:
                    plan["sell"].append({"price": est_star, "qty": q_q,
                                          "type": "동시호가",
                                          "desc": "⭐별값매도(1/4쿼터) [진입후 예상]"})
                if est_tgt > 0 and q_r > 0:
                    plan["sell"].append({"price": est_tgt, "qty": q_r,
                                          "type": "지정가",
                                          "desc": "🎯목표가매도(잔여) [진입후 예상]"})
            for o in plan["buy"]:
                res = self.broker.buy(code, o["qty"], price=o["price"],
                                      order_type=self.broker.ORDER_LIMIT)
                self._record_buy(code, name, pos, o["qty"], o["price"], o["desc"])
                log.info(f"[Engine] {name} {o['desc']}: {o['qty']}주 @{o['price']:,}원")
            return

        one_portion_qty = calc_one_portion_qty(p["one_portion"], avg_price)
        t_val      = calc_t_val(total_qty, one_portion_qty)
        star_ratio = calc_star_ratio(p["target_ratio"], p["split"], t_val)
        star_price = calc_star_price(avg_price, star_ratio)
        is_reverse = t_val > (p["split"] - 1)

        if is_reverse:
            self._loc_reverse_buy(sym, pos, t_val, avg_price, star_price,
                                   p["one_portion"], cur, large_num)
            return

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
            self.broker.buy(code, o["qty"], price=o["price"],
                            order_type=self.broker.ORDER_LIMIT)
            self._record_buy(code, name, pos, o["qty"], o["price"], o["desc"])
            log.info(f"[Engine] {name} {o['desc']}: {o['qty']}주 @{o['price']:,}원")
            pos = self.db.get_position(code) or pos

    def _loc_reverse_buy(self, sym, pos, t_val, avg_price,
                          star_price, one_portion_krw, cur, large_num):
        code    = sym.get("code", "")
        name    = sym.get("name", code)
        qty     = int(pos.get("total_qty", 0))
        rev_day = pos.get("reverse_day", 1)

        log.warning(f"[Engine] {name} 리버스 모드 {rev_day}일차 (t_val={t_val:.2f})")

        if rev_day == 1:
            sell_qty = max(1, math.floor(qty / 10))
            self.broker.sell(code, sell_qty, order_type=self.broker.ORDER_MARKET)
            self.db.record_trade(code=code, name=name, side="SELL",
                                 qty=sell_qty, price=cur, profit=0, profit_pct=0)
            self.notifier.send(
                f"🚨 <b>{name} 리버스 1일차</b>\n시장가 10% 매도: {sell_qty:,}주"
            )
            self.db.upsert_position(
                code=code, name=name, mode="INFINITE",
                round_no=pos.get("round_no", 1),
                avg_price=avg_price, total_qty=qty - sell_qty,
                reverse_day=2,
            )
        else:
            p_buy = max(1, star_price - get_tick_size(star_price))
            p_buy = min(p_buy, large_num)
            q_buy = math.floor(one_portion_krw / p_buy) if p_buy > 0 else 0
            if q_buy > 0:
                self.broker.buy(code, q_buy, price=p_buy,
                                order_type=self.broker.ORDER_LIMIT)
                self._record_buy(code, name, pos, q_buy, p_buy, "⚓리버스매수")

    def _record_buy(self, code, name, pos, qty, price, desc):
        pos     = pos or {}
        old_qty = int(pos.get("total_qty", 0))
        old_avg = int(pos.get("avg_price", 0))
        new_qty = old_qty + qty
        new_avg = (old_avg * old_qty + price * qty) // new_qty if new_qty > 0 else price
        self.db.upsert_position(code=code, name=name, mode="INFINITE",
                                round_no=pos.get("round_no", 1),
                                avg_price=new_avg, total_qty=new_qty)
        self.db.record_trade(code=code, name=name, side="BUY", qty=qty, price=price)
        self.notifier.notify_infinite_buy(
            code=code, name=name,
            qty=qty, price=price, amount=qty * price,
            desc=desc, avg_price=new_avg, total_qty=new_qty,
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
                    self._record_buy(code, s.get("name", code), pos, qty, cur,
                                     "🔔동시호가재주문")
            except Exception as e:
                log.exception(f"[Engine] {code} 동시호가 재주문 실패: {e}")

    # ----------------------------------------------------------
    # ④ V-REV 리밸런싱 — 구버전 호환 (09:10 콜)
    #    승승장군 원본은 15:10 LOC. 하지만 호환성을 위해
    #    vrev_loc()을 scheduler_trade에서 15:10에 호출하도록 변경
    # ----------------------------------------------------------
    async def vrev_rebalance(self, symbols: list):
        """Legacy 09:10 V-REV — 실제 로직은 vrev_loc()으로 이관됨."""
        if not symbols:
            return
        log.info("[Engine] V-REV 리밸런싱: 09:10은 상태 확인만 (실매매는 15:10 LOC)")
        for s in symbols:
            code = s.get("code", "")
            try:
                cur  = self.broker.get_current_price(code)
                sma5 = self._get_sma5(code)
                if cur > 0 and sma5 > 0:
                    pct = (cur - sma5) / sma5 * 100
                    log.info(
                        f"[Engine] V-REV {s.get('name')} 오전 상태: "
                        f"현재가 {cur:,} / SMA5 {sma5:,} ({pct:+.2f}%)"
                    )
            except Exception as e:
                log.debug(f"[Engine] V-REV 상태 확인 실패 {code}: {e}")

    # ----------------------------------------------------------
    # ⑤ /sync 주문계획 생성
    # ----------------------------------------------------------
    def build_sync_plan(self, sym: dict, cur: int, pos: dict) -> dict:
        p         = self._sym_params(sym)
        avg_price = int(pos.get("avg_price", 0)) if pos else 0
        total_qty = int(pos.get("total_qty", 0)) if pos else 0
        mode      = sym.get("mode", "INFINITE")

        # V-REV 모드
        if mode == "VREV":
            sma5      = self._get_sma5(sym.get("code", ""))
            pct_diff  = (cur - sma5) / sma5 * 100 if sma5 > 0 else 0
            direction = "🟢 매수 대기" if cur < sma5 else ("🔴 매도 대기" if cur > sma5 else "⚪ 중립")
            plan      = {"buy": [], "sell": []}

            if cur < sma5:
                buy_price = round_to_tick(cur)
                qty       = math.floor(
                    min(sym.get("daily_buy_limit_krw", p["one_portion"]),
                        p["one_portion"]) / buy_price
                ) if buy_price > 0 else 0
                if qty > 0:
                    plan["buy"].append({"price": buy_price, "qty": qty,
                                         "type": "동시호가",
                                         "desc": f"⚖️V-REV매수 (SMA5 {pct_diff:.1f}%)"})
            elif cur > sma5 and avg_price > 0 and total_qty > 0:
                pop_price = round_to_tick(int(avg_price * 1.006))
                sell_qty  = max(1, math.floor(total_qty / (p["split"] // 2 or 1)))
                plan["sell"].append({"price": pop_price, "qty": sell_qty,
                                      "type": "지정가",
                                      "desc": f"⚖️V-REV팝 (SMA5 {pct_diff:.1f}%)"})

            return {
                "phase":        f"⚖️V-REV {direction}",
                "t_val":        calc_t_val(total_qty,
                                           calc_one_portion_qty(p["one_portion"], avg_price))
                                if avg_price > 0 else 0.0,
                "star_ratio":   0.0,
                "star_price":   0,
                "target_price": round_to_tick(int(avg_price * 1.006)) if avg_price > 0 else 0,
                "large_num":    0,
                "sma5":         sma5,
                "pct_diff":     pct_diff,
                "plan":         plan,
            }

        # INFINITE 모드
        large_num = calc_large_num(cur, p["large_pct"]) if cur > 0 else 0

        if total_qty == 0 or avg_price == 0:
            plan = plan_new_entry(p["one_portion"], large_num)
            if cur > 0 and large_num > 0:
                est_qty  = math.floor(p["one_portion"] / large_num)
                sr       = calc_star_ratio(p["target_ratio"], p["split"], 1.0)
                est_star = calc_star_price(cur, sr)
                est_tgt  = calc_target_price(cur, p["target_ratio"])
                q_q = max(1, math.floor(est_qty * 0.25))
                q_r = max(0, est_qty - q_q)
                if est_star > 0 and q_q > 0:
                    plan["sell"].append({"price": est_star, "qty": q_q,
                                          "type": "동시호가",
                                          "desc": "⭐별값매도(1/4쿼터) [진입후 예상]"})
                if est_tgt > 0 and q_r > 0:
                    plan["sell"].append({"price": est_tgt, "qty": q_r,
                                          "type": "지정가",
                                          "desc": "🎯목표가매도(잔여) [진입후 예상]"})
                return {
                    "phase": "✨새출발",
                    "t_val": 0.0,
                    "star_ratio":  sr,
                    "star_price":  est_star,
                    "target_price": est_tgt,
                    "large_num":   large_num,
                    "plan": plan,
                }
            return {"phase": "✨새출발", "t_val": 0.0, "star_ratio": p["target_ratio"],
                    "star_price": 0, "target_price": 0, "large_num": large_num, "plan": plan}

        one_portion_qty = calc_one_portion_qty(p["one_portion"], avg_price)
        t_val        = calc_t_val(total_qty, one_portion_qty)
        star_ratio   = calc_star_ratio(p["target_ratio"], p["split"], t_val)
        star_price   = calc_star_price(avg_price, star_ratio)
        target_price = calc_target_price(avg_price, p["target_ratio"])
        is_rev       = t_val > (p["split"] - 1)
        half         = p["split"] / 2.0
        phase = ("🚨리버스" if is_rev
                 else "🌓전반전" if t_val < half
                 else "🌕후반전")

        plan = plan_loc_buy(
            t_val=t_val, split=p["split"],
            avg_price=avg_price, star_price=star_price,
            one_portion_krw=p["one_portion"], cur_price=cur,
            large_num=large_num,
            target_price=target_price, total_qty=total_qty,
        )
        return {
            "phase":        phase,
            "t_val":        t_val,
            "star_ratio":   star_ratio,
            "star_price":   star_price,
            "target_price": target_price,
            "large_num":    large_num,
            "plan":         plan,
        }

    # ----------------------------------------------------------
    # ⑥ 유틸
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
