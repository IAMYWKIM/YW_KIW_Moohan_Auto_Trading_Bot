# ==============================================================
# [avwap_engine.py] AVWAP 퀀트 엔진 v1.0
#
# 승승장군 AVWAP 듀얼 모멘텀 암살자 (V44~V79 집대성)
# 국내 ETF 정규장 전용 버전 (프리/에프터 없음)
#
# 원본 대비 국내 ETF 변환 포인트:
#   - 운영 시간: 09:30~14:30 KST (정규장만)
#   - 타임쉴드: 09:00~09:30 (개장 30분 후부터 교전)
#   - 강제청산: 15:20 (동시호가 전)
#   - 인버스 없음: 롱 단방향, 하락 모멘텀 시 관망
#   - VWAP: 정규장 09:00 기준 누적 계산
#   - 수수료: 0.015% (국내 ETF 기준) → 왕복 0.03%
#
# 전략 구조:
#   ① AVWAP 듀얼 모멘텀 스캔 (5분 주기)
#   ② ATR5 잔여체력 검증
#   ③ 다이나믹 목표가 자율주행
#   ④ 하드스탑 -8% / 타임스탑 15:20
#   ⑤ 다중 출장 (익절 후 쿨다운 → 재진입)
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
TIME_SHIELD_END   = datetime.time(9, 30)   # 타임쉴드 해제 (교전 시작)
TIME_NO_ENTRY     = datetime.time(14, 30)  # 신규 진입 금지
TIME_FORCE_EXIT   = datetime.time(15, 20)  # 강제 청산 (동시호가 전)
COMMISSION_RATE   = 0.00015                # 편도 수수료 0.015%
HARD_STOP_PCT     = -8.0                   # 하드스탑 -8%
COOLDOWN_MINUTES  = 5                      # 익절 후 쿨다운 (분)

# 다이나믹 목표가 (잔여체력 기반)
TARGET_TIERS = [
    (5.0, 5.0),  # 잔여체력 ≥ 5% → 목표 5%
    (4.0, 4.0),  # 잔여체력 ≥ 4% → 목표 4%
    (3.0, 3.0),  # 잔여체력 ≥ 3% → 목표 3%
    (0.0, 2.0),  # 그 외         → 목표 2% (최저 방어막)
]
MIN_TARGET_PCT  = 2.0   # RR 최저 방어막
MIN_ENERGY_PCT  = 2.0   # 최소 잔여체력 (미만 시 진입 금지)


# ==============================================================
# 데이터 구조
# ==============================================================
@dataclass
class AVWAPState:
    """종목별 AVWAP 엔진 상태."""
    code:             str
    name:             str
    budget:           int       = 0      # 할당 예산 (원)

    # 포지션
    position_qty:     int       = 0
    position_avg:     int       = 0
    position_time:    Optional[datetime.datetime] = None

    # 당일 상태
    day_locked:       bool      = False  # 하드스탑 후 당일 동결
    today_pnl:        int       = 0      # 당일 실현손익
    today_trades:     int       = 0      # 당일 출장 횟수
    last_exit_time:   Optional[datetime.datetime] = None  # 마지막 청산 시각

    # VWAP 계산용 누적값
    vwap_cum_pv:      float     = 0.0   # 누적 가격×거래량
    vwap_cum_vol:     int       = 0     # 누적 거래량
    vwap_5m_history: list      = field(default_factory=list)  # 5분 VWAP 기록

    # 전일 VWAP
    prev_day_vwap:    float     = 0.0

    # ATR5
    atr5:             float     = 0.0
    day_low:          int       = 999999999
    day_high:         int       = 0

    def reset_day(self):
        """매일 장 시작 시 일일 상태 초기화."""
        self.day_locked    = False
        self.today_pnl     = 0
        self.today_trades  = 0
        self.last_exit_time= None
        self.vwap_cum_pv   = 0.0
        self.vwap_cum_vol  = 0
        self.vwap_5m_history = []
        self.day_low       = 999999999
        self.day_high      = 0


# ==============================================================
# 핵심 계산 함수
# ==============================================================

def calc_vwap(cum_pv: float, cum_vol: int) -> float:
    """VWAP = 누적(가격×거래량) / 누적거래량."""
    if cum_vol <= 0:
        return 0.0
    return cum_pv / cum_vol


def calc_5m_avg_vwap(vwap_history: list, n: int = 5) -> float:
    """최근 n개 5분 VWAP 평균."""
    if not vwap_history:
        return 0.0
    recent = vwap_history[-n:] if len(vwap_history) >= n else vwap_history
    return sum(recent) / len(recent)


def calc_remaining_energy(cur_price: int, day_low: int, atr5: float) -> float:
    """
    잔여체력(%) = ATR5% - 저가 대비 상승폭%
    → 오늘 저가에서 얼마나 올라왔는지 차감한 남은 에너지
    """
    if atr5 <= 0 or day_low <= 0 or cur_price <= 0:
        return 0.0
    atr5_pct      = atr5 / cur_price * 100
    used_energy   = (cur_price - day_low) / cur_price * 100
    return max(0.0, atr5_pct - used_energy)


def calc_dynamic_target(remaining_energy: float) -> float:
    """
    잔여체력 기반 다이나믹 목표가 결정 (v44 핵심).
    잔여체력 ≥ 5% → 5% / ≥ 4% → 4% / ≥ 3% → 3% / 기타 → 2%
    """
    for threshold, target in TARGET_TIERS:
        if remaining_energy >= threshold:
            return target
    return MIN_TARGET_PCT


def check_entry_signal(
    intraday_vwap: float,
    prev_day_vwap: float,
    vwap_5m_avg:   float,
    remaining_energy: float,
) -> tuple[bool, str]:
    """
    AVWAP 듀얼 모멘텀 진입 조건 검증.

    조건:
    ① 당일 VWAP > 전일 VWAP  (모멘텀 상승)
    ② 현재 VWAP > 5분 평균 VWAP  (단기 추세 상승)
    ③ 잔여체력 > MIN_ENERGY_PCT  (과열 방지)

    반환: (진입 가능 여부, 사유)
    """
    if prev_day_vwap <= 0:
        return False, "전일 VWAP 없음"
    if intraday_vwap <= 0:
        return False, "당일 VWAP 없음"

    cond1 = intraday_vwap > prev_day_vwap
    cond2 = intraday_vwap > vwap_5m_avg if vwap_5m_avg > 0 else True
    cond3 = remaining_energy >= MIN_ENERGY_PCT

    if not cond1:
        return False, f"모멘텀 부족 (당일VWAP {intraday_vwap:.0f} ≤ 전일VWAP {prev_day_vwap:.0f})"
    if not cond2:
        return False, f"단기추세 역방향 (VWAP {intraday_vwap:.0f} ≤ 5MA {vwap_5m_avg:.0f})"
    if not cond3:
        return False, f"잔여체력 부족 ({remaining_energy:.1f}% < {MIN_ENERGY_PCT}%)"

    return True, f"진입 가능 (잔여체력 {remaining_energy:.1f}%)"


def is_trading_time(now: datetime.datetime) -> str:
    """
    현재 시각에 따른 교전 상태 반환.
    'SHIELD' / 'ACTIVE' / 'NO_ENTRY' / 'FORCE_EXIT' / 'CLOSED'
    """
    if now.weekday() >= 5:
        return "CLOSED"
    t = now.time()
    if t < datetime.time(9, 0):
        return "CLOSED"
    if t < TIME_SHIELD_END:
        return "SHIELD"   # 09:00~09:30 타임쉴드
    if t < TIME_NO_ENTRY:
        return "ACTIVE"   # 09:30~14:30 교전 가능
    if t < TIME_FORCE_EXIT:
        return "NO_ENTRY" # 14:30~15:20 포지션 청산만
    if t < datetime.time(15, 30):
        return "FORCE_EXIT" # 15:20~15:30 강제 청산
    return "CLOSED"


# ==============================================================
# AVWAPEngine 클래스
# ==============================================================
class AVWAPEngine:
    """
    국내 ETF AVWAP 퀀트 엔진.

    사용법:
        engine = AVWAPEngine(broker, db, notifier)
        engine.init_symbols([{"code":"488080","name":"...","avwap_budget":1000000}])
        # 스케줄러에서 5분마다 호출:
        await engine.on_tick(code, cur_price, volume)
    """

    def __init__(self, broker, db, notifier):
        self.broker   = broker
        self.db       = db
        self.notifier = notifier
        self.states:  dict[str, AVWAPState] = {}

    def init_symbols(self, symbols: list):
        """AVWAP 운용 종목 초기화."""
        for s in symbols:
            code = s["code"]
            if code not in self.states:
                self.states[code] = AVWAPState(
                    code   = code,
                    name   = s.get("name", code),
                    budget = s.get("avwap_budget", 0),
                )

    def morning_reset(self, prev_day_vwap_map: dict = None):
        """매일 09:00 장 시작 전 호출 — 일일 상태 초기화."""
        for code, st in self.states.items():
            # 전일 VWAP 저장 후 초기화
            if prev_day_vwap_map and code in prev_day_vwap_map:
                st.prev_day_vwap = prev_day_vwap_map[code]
            elif st.vwap_cum_vol > 0:
                st.prev_day_vwap = calc_vwap(st.vwap_cum_pv, st.vwap_cum_vol)

            st.reset_day()
            log.info(
                f"[AVWAP] {st.name} 일일 초기화 | 전일VWAP: {st.prev_day_vwap:.0f}원"
            )

    def update_atr5(self, code: str, atr5: float):
        """ATR5 업데이트 (매일 장 전 또는 9시 초기화 시)."""
        if code in self.states:
            self.states[code].atr5 = atr5

    # ----------------------------------------------------------
    # 틱 처리 — 5분 주기 스케줄러에서 호출
    # ----------------------------------------------------------
    async def on_tick(self, code: str, cur_price: int, volume: int):
        """
        5분 주기 가격/거래량 업데이트 + 진입/청산 판단.
        """
        if code not in self.states:
            return
        st  = self.states[code]
        now = datetime.datetime.now(KST)
        status = is_trading_time(now)

        # VWAP 누적
        if status in ("ACTIVE", "NO_ENTRY", "SHIELD"):
            st.vwap_cum_pv  += cur_price * volume
            st.vwap_cum_vol += volume
            st.day_low   = min(st.day_low,  cur_price)
            st.day_high  = max(st.day_high, cur_price)

            intraday_vwap = calc_vwap(st.vwap_cum_pv, st.vwap_cum_vol)
            if status != "SHIELD":
                st.vwap_5m_history.append(intraday_vwap)

        # 강제 청산
        if status == "FORCE_EXIT":
            if st.position_qty > 0:
                await self._force_exit(st, cur_price, "타임스탑 15:20")
            return

        if status not in ("ACTIVE", "NO_ENTRY"):
            return
        if st.day_locked:
            return

        intraday_vwap  = calc_vwap(st.vwap_cum_pv, st.vwap_cum_vol)
        vwap_5m_avg    = calc_5m_avg_vwap(st.vwap_5m_history)
        remaining_pct  = calc_remaining_energy(cur_price, st.day_low, st.atr5)
        dyn_target_pct = calc_dynamic_target(remaining_pct)

        # ── 포지션 있으면 청산 조건 체크 ───────────────────────
        if st.position_qty > 0 and st.position_avg > 0:
            profit_pct = (cur_price - st.position_avg) / st.position_avg * 100
            target_px  = round(st.position_avg * (1 + dyn_target_pct / 100))
            stop_px    = round(st.position_avg * (1 + HARD_STOP_PCT / 100))

            if cur_price >= target_px:
                await self._exit_position(st, cur_price, profit_pct, "익절")
                return

            if cur_price <= stop_px:
                await self._exit_position(st, cur_price, profit_pct, "하드스탑")
                st.day_locked = True
                loss = (cur_price - st.position_avg) * st.position_qty
                await asyncio.to_thread(
                    self.notifier.notify_avwap_locked,
                    st.code, st.name, cur_price, loss,
                )
                return

            # 정보 로그
            log.debug(
                f"[AVWAP] {st.name} 보유중 | 현재:{cur_price:,} 평단:{st.position_avg:,} "
                f"수익:{profit_pct:+.2f}% 목표:{target_px:,} 스탑:{stop_px:,}"
            )
            return

        # ── 진입 조건 체크 ─────────────────────────────────────
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
            await self._enter_position(st, cur_price, dyn_target_pct, remaining_pct)
        else:
            log.debug(f"[AVWAP] {st.name} 진입 보류: {reason}")

    # ----------------------------------------------------------
    # 진입
    # ----------------------------------------------------------
    async def _enter_position(
        self, st: AVWAPState, cur_price: int,
        target_pct: float, remaining_pct: float
    ):
        if st.budget <= 0 or cur_price <= 0:
            return

        from kiwoom_api import round_to_tick
        qty = math.floor(st.budget / cur_price)
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
            st.position_qty   = qty
            st.position_avg   = buy_price
            st.position_time  = datetime.datetime.now(KST)
            st.today_trades  += 1

            intraday_vwap = calc_vwap(st.vwap_cum_pv, st.vwap_cum_vol)
            await asyncio.to_thread(
                self.notifier.notify_avwap_entry,
                st.code, st.name, qty, buy_price, qty * buy_price,
                target_pct, HARD_STOP_PCT, remaining_pct,
                intraday_vwap, st.prev_day_vwap,
                st.today_trades,
            )
            log.info(
                f"[AVWAP] {st.name} 진입 | {buy_price:,}원 × {qty}주 "
                f"목표:{target_pct:.1f}% 잔여:{remaining_pct:.1f}%"
            )
        except Exception as e:
            log.exception(f"[AVWAP] {st.name} 진입 실패: {e}")

    # ----------------------------------------------------------
    # 청산
    # ----------------------------------------------------------
    async def _exit_position(
        self, st: AVWAPState, cur_price: int, profit_pct: float, reason: str
    ):
        if st.position_qty <= 0:
            return
        try:
            res = await asyncio.to_thread(
                self.broker.sell, st.code, st.position_qty, 0,
                self.broker.ORDER_MARKET
            )
            profit = (cur_price - st.position_avg) * st.position_qty
            st.today_pnl   += profit
            st.last_exit_time = datetime.datetime.now(KST)

            await asyncio.to_thread(
                self.notifier.notify_avwap_exit,
                st.code, st.name,
                st.position_qty, cur_price,
                profit, profit_pct,
                reason, st.today_pnl, st.today_trades,
            )

            self.db.record_trade(
                code=st.code, name=st.name, side="SELL",
                qty=st.position_qty, price=cur_price,
                profit=profit, profit_pct=profit_pct,
                order_no=res.get("order_no", ""),
            )
            log.info(
                f"[AVWAP] {st.name} {reason} | {cur_price:,}원 "
                f"{sign}{profit_pct:.2f}%"
            )
        except Exception as e:
            log.exception(f"[AVWAP] {st.name} 청산 실패: {e}")
        finally:
            st.position_qty  = 0
            st.position_avg  = 0
            st.position_time = None

    async def _force_exit(self, st: AVWAPState, cur_price: int, reason: str):
        if st.position_qty <= 0:
            return
        profit_pct = (
            (cur_price - st.position_avg) / st.position_avg * 100
            if st.position_avg > 0 else 0
        )
        await self._exit_position(st, cur_price, profit_pct, reason)

    # ----------------------------------------------------------
    # /avwap 텔레봇 상태 조회
    # ----------------------------------------------------------
    def get_status_text(self, code: str = None) -> str:
        codes = [code] if code else list(self.states.keys())
        lines = ["⚡️ <b>AVWAP 퀀트 엔진 현황</b>", ""]
        now = datetime.datetime.now(KST)
        status = is_trading_time(now)

        lines.append(f"🕐 {now.strftime('%H:%M:%S')} KST | {self._status_icon(status)}")
        lines.append("")

        for c in codes:
            if c not in self.states:
                continue
            st = self.states[c]
            intraday_vwap = calc_vwap(st.vwap_cum_pv, st.vwap_cum_vol)
            vwap_5m_avg   = calc_5m_avg_vwap(st.vwap_5m_history)
            remaining_pct = 0.0

            lock_icon = "🔒 당일동결" if st.day_locked else "🟢 교전가능"
            lines += [
                f"💎 <b>{st.name}</b> ({c})",
                f"  상태: {lock_icon}",
                f"  예산: {st.budget:,}원",
                f"  전일VWAP: {st.prev_day_vwap:,.0f}원",
                f"  당일VWAP: {intraday_vwap:,.0f}원",
                f"  5MA VWAP: {vwap_5m_avg:,.0f}원",
                f"  ATR5: {st.atr5:,.0f}원",
            ]
            if st.position_qty > 0:
                lines += [
                    f"  📊 보유: {st.position_qty:,}주 @ {st.position_avg:,}원",
                ]
            lines += [
                f"  💰 당일손익: {st.today_pnl:+,}원  출장: {st.today_trades}회",
                "",
            ]
        return "\n".join(lines)

    @staticmethod
    def _status_icon(status: str) -> str:
        return {
            "SHIELD":     "🛡️ 타임쉴드 (09:00~09:30)",
            "ACTIVE":     "⚔️ 교전 가능 (09:30~14:30)",
            "NO_ENTRY":   "🚫 신규진입 금지 (14:30~)",
            "FORCE_EXIT": "🔔 강제청산 (15:20~)",
            "CLOSED":     "🌙 장 마감",
        }.get(status, status)
