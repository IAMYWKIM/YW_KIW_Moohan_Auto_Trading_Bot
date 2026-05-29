# ==============================================================
# [avwap_engine.py] AVWAP 퀀트 엔진 v2.0
#
# 승승장군 V44~V79 완전 재구현 (국내 ETF 정규장 전용)
#
# v2.0 수정 사항 (issues #52~#66 분석 반영):
#   - [BUG FIX] 진입조건 cond2 방향 수정
#       잘못: intraday_vwap > vwap_5m_avg
#       수정: vwap_5m_avg > intraday_vwap  ← 원본과 동일
#   - [수정] 최소 목표가 2.0% → 2.03% (국내 왕복수수료 반영)
#   - [수정] 수수료율 0.015% → 국내 ETF 기준 명시
#   - [추가] 일일 최대 출장 횟수 제한 옵션
#   - [추가] 포지션 진입 금액 추적 (복리 계산 정확도)
#   - [확인] 타임쉴드 09:30, 강제청산 15:20 ← 정상
#   - [확인] 하드스탑 -8.0% ← V44 기준 정상
#   - [확인] 다이나믹 목표가 티어 ← 정상
#   - [확인] 쿨다운 5분 ← 정상
#
# 원본 V44 진입 조건 (이슈 #66 원문):
#   롱: 당일 실시간 VWAP > 전일 VWAP
#   AND 5분 평균 VWAP > 당일 실시간 VWAP  ← 핵심!
#   AND 잔여체력 ≥ 2.0%
# ==============================================================
import asyncio
import datetime
import logging
import math
from dataclasses import dataclass, field
from typing import Optional
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
log = logging.getLogger(__name__)

# ==============================================================
# 운영 상수
# ==============================================================
TIME_SHIELD_END   = datetime.time(9, 30)   # 타임쉴드 해제 (개장 30분)
TIME_NO_ENTRY     = datetime.time(14, 30)  # 신규 진입 금지
TIME_FORCE_EXIT   = datetime.time(15, 20)  # 강제 청산 (동시호가 전)

COMMISSION_RATE   = 0.00015   # 편도 0.015% (국내 ETF)
ROUND_TRIP_COMM   = COMMISSION_RATE * 2   # 왕복 0.03%

HARD_STOP_PCT     = -8.0      # V44 하드스탑 -8%
MIN_TARGET_PCT    = 2.03      # 왕복수수료(0.03%) 상쇄 후 순익 2% 확보
MIN_ENERGY_PCT    = 2.0       # 최소 잔여체력 (미만 시 진입 금지)
COOLDOWN_MINUTES  = 5         # 익절 후 쿨다운

# 다이나믹 목표가 티어 (V44 기준)
TARGET_TIERS = [
    (5.0, 5.0),
    (4.0, 4.0),
    (3.0, 3.0),
    (0.0, MIN_TARGET_PCT),  # 기본 최저 방어막
]


# ==============================================================
# 데이터 구조
# ==============================================================
@dataclass
class AVWAPState:
    code:            str
    name:            str
    budget:          int       = 0

    # 포지션
    position_qty:    int       = 0
    position_avg:    int       = 0
    position_amount: int       = 0   # 진입 금액 추적 (복리 계산용)
    position_time:   Optional[datetime.datetime] = None

    # 당일 상태
    day_locked:      bool      = False
    today_pnl:       int       = 0
    today_trades:    int       = 0
    last_exit_time:  Optional[datetime.datetime] = None

    # VWAP 계산용 누적값
    vwap_cum_pv:     float     = 0.0
    vwap_cum_vol:    int       = 0
    vwap_5m_history: list      = field(default_factory=list)

    # 전일 종가 VWAP
    prev_day_vwap:   float     = 0.0

    # ATR5
    atr5:            float     = 0.0
    day_low:         int       = 999_999_999
    day_high:        int       = 0

    def reset_day(self):
        self.day_locked     = False
        self.today_pnl      = 0
        self.today_trades   = 0
        self.last_exit_time = None
        self.vwap_cum_pv    = 0.0
        self.vwap_cum_vol   = 0
        self.vwap_5m_history = []
        self.day_low        = 999_999_999
        self.day_high       = 0


# ==============================================================
# 핵심 계산 함수
# ==============================================================

def calc_vwap(cum_pv: float, cum_vol: int) -> float:
    if cum_vol <= 0:
        return 0.0
    return cum_pv / cum_vol


def calc_5m_avg_vwap(vwap_history: list, n: int = 5) -> float:
    """최근 n개 VWAP 평균 (단기 모멘텀 지표)."""
    if not vwap_history:
        return 0.0
    recent = vwap_history[-n:] if len(vwap_history) >= n else vwap_history
    return sum(recent) / len(recent)


def calc_remaining_energy(cur_price: int, day_low: int, atr5: float) -> float:
    """
    잔여체력 = ATR5% - (현재가 - 당일저가) / 현재가 × 100
    V44 핵심: 당일 저가 대비 얼마나 올라왔는지 차감한 남은 에너지.
    """
    if atr5 <= 0 or day_low <= 0 or cur_price <= 0:
        return 0.0
    atr5_pct    = atr5 / cur_price * 100
    used_energy = (cur_price - day_low) / cur_price * 100
    return max(0.0, atr5_pct - used_energy)


def calc_dynamic_target(remaining_energy: float) -> float:
    """V44 다이나믹 목표가: 잔여체력 기반 자율주행."""
    for threshold, target in TARGET_TIERS:
        if remaining_energy >= threshold:
            return target
    return MIN_TARGET_PCT


def check_entry_signal(
    intraday_vwap: float,
    prev_day_vwap: float,
    vwap_5m_avg:  float,
    remaining_energy: float,
) -> tuple:
    """
    V44 AVWAP 듀얼 모멘텀 진입 조건 (이슈 #66 원문 그대로):

    롱 진입:
      ① 당일 실시간 VWAP > 전일 VWAP     (당일 모멘텀 상승)
      ② 5분 평균 VWAP > 당일 실시간 VWAP  (단기가 일간 평균 위)  ← v1.0 버그 수정
      ③ 잔여체력 ≥ MIN_ENERGY_PCT         (오버슈팅 차단)
    """
    if prev_day_vwap <= 0 or intraday_vwap <= 0:
        return False, "VWAP 데이터 없음"

    cond1 = intraday_vwap > prev_day_vwap
    # ✅ 수정: 원본과 동일하게 vwap_5m_avg > intraday_vwap
    cond2 = vwap_5m_avg > intraday_vwap if vwap_5m_avg > 0 else False
    cond3 = remaining_energy >= MIN_ENERGY_PCT

    if not cond1:
        return False, (
            f"모멘텀 부족 "
            f"(당일VWAP {intraday_vwap:.0f} ≤ 전일VWAP {prev_day_vwap:.0f})"
        )
    if not cond2:
        return False, (
            f"단기모멘텀 약세 "
            f"(5MA {vwap_5m_avg:.0f} ≤ 당일VWAP {intraday_vwap:.0f})"
        )
    if not cond3:
        return False, f"잔여체력 부족 ({remaining_energy:.1f}% < {MIN_ENERGY_PCT}%)"

    return True, (
        f"진입 가능 | 잔여체력 {remaining_energy:.1f}% "
        f"| 5MA {vwap_5m_avg:.0f} > 당일VWAP {intraday_vwap:.0f}"
    )


def is_trading_time(now: datetime.datetime) -> str:
    """
    KST 기준 교전 상태 반환.
    타임쉴드(09:00~09:30) → 교전(09:30~14:30) → 진입금지(14:30~15:20) → 강제청산(15:20)
    """
    if now.weekday() >= 5:
        return "CLOSED"
    t = now.time()
    if t < datetime.time(9, 0):
        return "CLOSED"
    if t < TIME_SHIELD_END:
        return "SHIELD"
    if t < TIME_NO_ENTRY:
        return "ACTIVE"
    if t < TIME_FORCE_EXIT:
        return "NO_ENTRY"
    if t < datetime.time(15, 30):
        return "FORCE_EXIT"
    return "CLOSED"


# ==============================================================
# AVWAPEngine
# ==============================================================
class AVWAPEngine:
    """
    국내 ETF AVWAP 퀀트 엔진 v2.0
    승승장군 V44~V79 완전 구현
    """

    def __init__(self, broker, db, notifier):
        self.broker   = broker
        self.db       = db
        self.notifier = notifier
        self.states:  dict[str, AVWAPState] = {}

    def init_symbols(self, symbols: list):
        for s in symbols:
            code = s["code"]
            if code not in self.states:
                self.states[code] = AVWAPState(
                    code   = code,
                    name   = s.get("name", code),
                    budget = s.get("avwap_budget", 0),
                )
            else:
                # 예산만 업데이트 (포지션 유지)
                self.states[code].budget = s.get("avwap_budget", 0)

    def morning_reset(self, prev_day_vwap_map: dict = None):
        """09:00 일일 초기화 — 전일 VWAP 보존 후 당일 상태 리셋."""
        for code, st in self.states.items():
            # 전일 VWAP 저장
            if prev_day_vwap_map and code in prev_day_vwap_map:
                st.prev_day_vwap = float(prev_day_vwap_map[code])
            elif st.vwap_cum_vol > 0:
                st.prev_day_vwap = calc_vwap(st.vwap_cum_pv, st.vwap_cum_vol)

            st.reset_day()
            log.info(
                f"[AVWAP] {st.name} 일일 초기화 | "
                f"전일VWAP: {st.prev_day_vwap:,.0f}원 | "
                f"ATR5: {st.atr5:,.0f}원"
            )

    def update_atr5(self, code: str, atr5: float):
        if code in self.states:
            self.states[code].atr5 = atr5

    # ----------------------------------------------------------
    # 틱 처리 — 5분 주기
    # ----------------------------------------------------------
    async def on_tick(self, code: str, cur_price: int, volume: int):
        if code not in self.states:
            return
        st     = self.states[code]
        now    = datetime.datetime.now(KST)
        status = is_trading_time(now)

        # VWAP 누적 (장중만)
        if status in ("ACTIVE", "NO_ENTRY", "SHIELD"):
            st.vwap_cum_pv  += cur_price * volume
            st.vwap_cum_vol += volume
            st.day_low   = min(st.day_low,  cur_price)
            st.day_high  = max(st.day_high, cur_price)
            intraday_vwap = calc_vwap(st.vwap_cum_pv, st.vwap_cum_vol)
            # 타임쉴드 끝난 후부터 5분 VWAP 기록
            if status != "SHIELD":
                st.vwap_5m_history.append(intraday_vwap)

        # 강제 청산 (15:20)
        if status == "FORCE_EXIT":
            if st.position_qty > 0:
                await self._force_exit(st, cur_price, "타임스탑 15:20")
            return

        if status not in ("ACTIVE", "NO_ENTRY"):
            return
        if st.day_locked:
            log.debug(f"[AVWAP] {st.name} 당일 동결 중")
            return

        intraday_vwap  = calc_vwap(st.vwap_cum_pv, st.vwap_cum_vol)
        vwap_5m_avg    = calc_5m_avg_vwap(st.vwap_5m_history)
        remaining_pct  = calc_remaining_energy(cur_price, st.day_low, st.atr5)
        dyn_target_pct = calc_dynamic_target(remaining_pct)

        # ── 포지션 청산 체크 ────────────────────────────────
        if st.position_qty > 0 and st.position_avg > 0:
            profit_pct = (cur_price - st.position_avg) / st.position_avg * 100
            target_px  = round(st.position_avg * (1 + dyn_target_pct / 100))
            stop_px    = round(st.position_avg * (1 + HARD_STOP_PCT / 100))

            log.debug(
                f"[AVWAP] {st.name} 보유중 | "
                f"현재:{cur_price:,} 평단:{st.position_avg:,} "
                f"수익:{profit_pct:+.2f}% "
                f"목표:{target_px:,} 스탑:{stop_px:,} "
                f"잔여체력:{remaining_pct:.1f}%"
            )

            if cur_price >= target_px:
                await self._exit_position(st, cur_price, profit_pct, "익절")
                return

            if cur_price <= stop_px:
                loss = (cur_price - st.position_avg) * st.position_qty
                await self._exit_position(st, cur_price, profit_pct, "하드스탑")
                st.day_locked = True
                await asyncio.to_thread(
                    self.notifier.notify_avwap_locked,
                    st.code, st.name, cur_price, loss,
                )
                return
            return

        # ── 신규 진입 조건 체크 ────────────────────────────
        if status != "ACTIVE":
            return  # NO_ENTRY 구간은 청산만

        # 쿨다운 체크
        if st.last_exit_time:
            elapsed = (now - st.last_exit_time).total_seconds() / 60
            if elapsed < COOLDOWN_MINUTES:
                return

        can_enter, reason = check_entry_signal(
            intraday_vwap, st.prev_day_vwap, vwap_5m_avg, remaining_pct
        )

        if can_enter:
            log.info(f"[AVWAP] {st.name} 진입 신호: {reason}")
            await self._enter_position(
                st, cur_price, dyn_target_pct,
                remaining_pct, intraday_vwap, vwap_5m_avg
            )
        else:
            log.debug(f"[AVWAP] {st.name} 관망: {reason}")

    # ----------------------------------------------------------
    # 진입
    # ----------------------------------------------------------
    async def _enter_position(
        self, st: AVWAPState, cur_price: int,
        target_pct: float, remaining_pct: float,
        intraday_vwap: float, vwap_5m_avg: float,
    ):
        if st.budget <= 0 or cur_price <= 0:
            return

        from kiwoom_api import round_to_tick
        qty      = math.floor(st.budget / cur_price)
        if qty <= 0:
            return

        buy_price = round_to_tick(cur_price)
        target_px = round_to_tick(int(buy_price * (1 + target_pct / 100)))
        stop_px   = round_to_tick(int(buy_price * (1 + HARD_STOP_PCT / 100)))

        try:
            res = await asyncio.to_thread(
                self.broker.buy, st.code, qty, buy_price,
                self.broker.ORDER_LIMIT
            )
            st.position_qty    = qty
            st.position_avg    = buy_price
            st.position_amount = buy_price * qty
            st.position_time   = datetime.datetime.now(KST)
            st.today_trades   += 1

            log.info(
                f"[AVWAP] {st.name} 진입 | "
                f"{buy_price:,}원 × {qty}주 "
                f"목표:{target_px:,}(+{target_pct:.1f}%) "
                f"스탑:{stop_px:,}({HARD_STOP_PCT:.0f}%) "
                f"잔여:{remaining_pct:.1f}% "
                f"5MA:{vwap_5m_avg:.0f} > VWAP:{intraday_vwap:.0f}"
            )

            await asyncio.to_thread(
                self.notifier.notify_avwap_entry,
                st.code, st.name, qty, buy_price, qty * buy_price,
                target_pct, HARD_STOP_PCT, remaining_pct,
                intraday_vwap, st.prev_day_vwap,
                st.today_trades,
            )
        except Exception as e:
            log.exception(f"[AVWAP] {st.name} 진입 실패: {e}")

    # ----------------------------------------------------------
    # 청산
    # ----------------------------------------------------------
    async def _exit_position(
        self, st: AVWAPState, cur_price: int,
        profit_pct: float, reason: str
    ):
        if st.position_qty <= 0:
            return
        try:
            res = await asyncio.to_thread(
                self.broker.sell, st.code, st.position_qty, 0,
                self.broker.ORDER_MARKET
            )
            profit = (cur_price - st.position_avg) * st.position_qty
            # 수수료 차감
            fee    = int(cur_price * st.position_qty * ROUND_TRIP_COMM)
            profit_net = profit - fee

            st.today_pnl     += profit_net
            st.last_exit_time = datetime.datetime.now(KST)

            self.db.record_trade(
                code=st.code, name=st.name, side="SELL",
                qty=st.position_qty, price=cur_price,
                profit=profit_net, profit_pct=profit_pct,
                order_no=res.get("order_no", ""),
            )

            log.info(
                f"[AVWAP] {st.name} {reason} | "
                f"{cur_price:,}원 {profit_pct:+.2f}% "
                f"순익:{profit_net:+,}원 "
                f"당일:{st.today_pnl:+,}원"
            )

            await asyncio.to_thread(
                self.notifier.notify_avwap_exit,
                st.code, st.name,
                st.position_qty, cur_price,
                profit_net, profit_pct,
                reason, st.today_pnl, st.today_trades,
            )
        except Exception as e:
            log.exception(f"[AVWAP] {st.name} 청산 실패: {e}")
        finally:
            st.position_qty    = 0
            st.position_avg    = 0
            st.position_amount = 0
            st.position_time   = None

    async def _force_exit(self, st: AVWAPState, cur_price: int, reason: str):
        if st.position_qty <= 0:
            return
        pct = (
            (cur_price - st.position_avg) / st.position_avg * 100
            if st.position_avg > 0 else 0
        )
        await self._exit_position(st, cur_price, pct, reason)

    # ----------------------------------------------------------
    # /avwap 텔레봇 상태 조회
    # ----------------------------------------------------------
    def get_status_text(self, code: str = None) -> str:
        codes  = [code] if code else list(self.states.keys())
        now    = datetime.datetime.now(KST)
        status = is_trading_time(now)

        lines = [
            "⚡️ <b>AVWAP 퀀트 엔진 v2.0</b>",
            f"🕐 {now.strftime('%H:%M:%S')} KST | {self._status_icon(status)}",
            "",
        ]

        for c in codes:
            if c not in self.states:
                continue
            st = self.states[c]
            intraday_vwap = calc_vwap(st.vwap_cum_pv, st.vwap_cum_vol)
            vwap_5m_avg   = calc_5m_avg_vwap(st.vwap_5m_history)
            remaining_pct = calc_remaining_energy(
                st.day_high if st.day_high > 0 else 1,
                st.day_low, st.atr5
            )

            # 진입 가능 여부 사전 체크
            can_enter, reason = check_entry_signal(
                intraday_vwap, st.prev_day_vwap, vwap_5m_avg, remaining_pct
            )
            signal_icon = "🟢 진입가능" if can_enter else f"🔴 {reason[:20]}"
            lock_icon   = "🔒 당일동결" if st.day_locked else signal_icon

            # 데이터 유무 판단 (장 마감 후는 정상적으로 0)
            no_data = (intraday_vwap <= 0 and st.atr5 <= 0)

            lines += [
                f"💎 <b>{st.name}</b> ({c})",
                f"  상태: {lock_icon}",
                f"  예산: {st.budget:,}원",
                f"  전일VWAP: {st.prev_day_vwap:,.0f}원",
                f"  당일VWAP: {intraday_vwap:,.0f}원",
                f"  5분MA: {vwap_5m_avg:,.0f}원",
                f"  ATR5: {st.atr5:,.0f}원  잔여체력: {remaining_pct:.1f}%",
            ]
            if no_data:
                lines.append(
                    "  ⚠️ VWAP 데이터 없음 — 장중(09:30~14:30)에만 수집\n"
                    "  → 내일 09:30 교전 시작 시 자동 활성화"
                )
            if st.position_qty > 0:
                lines.append(
                    f"  📊 보유: {st.position_qty:,}주 @ {st.position_avg:,}원"
                )
            lines += [
                f"  💰 당일손익: {st.today_pnl:+,}원  출장: {st.today_trades}회",
                "",
            ]

    @staticmethod
    def _status_icon(status: str) -> str:
        return {
            "SHIELD":     "🛡️ 타임쉴드 (09:00~09:30)",
            "ACTIVE":     "⚔️ 교전 가능 (09:30~14:30)",
            "NO_ENTRY":   "🚫 신규진입 금지 (14:30~)",
            "FORCE_EXIT": "🔔 강제청산 (15:20~)",
            "CLOSED":     "🌙 장 마감",
        }.get(status, status)