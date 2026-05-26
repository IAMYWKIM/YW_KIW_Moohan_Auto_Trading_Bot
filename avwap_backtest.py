# ==============================================================
# [avwap_backtest.py] AVWAP 전략 백테스트
#
# KODEX 반도체TOP10레버리지 (488080) 기반 시뮬레이션
# ※ 서버에서 실행 시 pykrx 실제 데이터로 교체 가능
#
# 실행: python3 avwap_backtest.py
# ==============================================================
import math
import random
import datetime
from dataclasses import dataclass
from typing import List, Dict

from avwap_engine import (
    calc_vwap, calc_5m_avg_vwap, calc_remaining_energy,
    calc_dynamic_target, check_entry_signal,
    HARD_STOP_PCT, MIN_TARGET_PCT, COMMISSION_RATE,
    TIME_SHIELD_END, TIME_NO_ENTRY, TIME_FORCE_EXIT,
    COOLDOWN_MINUTES,
)

# ==============================================================
# 시뮬레이션 데이터 생성
# (실제 데이터 사용 시 이 부분을 pykrx/naver API로 교체)
# ==============================================================

def generate_krx_etf_days(
    start_price: int = 80000,
    n_days:      int = 504,  # 약 2년 (252거래일 × 2)
    daily_vol:   float = 0.03,  # 일간 변동성 3% (2배 레버리지 기준)
    seed:        int = 42,
) -> List[Dict]:
    """
    국내 2배 레버리지 ETF 일봉 + 장중 5분봉 시뮬레이션.

    KODEX 반도체TOP10레버리지 (488080) 실제 특성:
    - 일간 변동성: 2~4%
    - ATR5:        일간변동성 × sqrt(5) ≈ 4~9%
    - 기준가:      2024년 초 약 70,000~90,000원 수준
    """
    random.seed(seed)
    days = []
    price = start_price

    # 2024-01-02 ~ 약 2년치
    base_date = datetime.date(2024, 1, 2)

    for d in range(n_days):
        # 주말 건너뜀
        cur_date = base_date + datetime.timedelta(days=d)
        while cur_date.weekday() >= 5:
            d += 1
            cur_date = base_date + datetime.timedelta(days=d)

        # 당일 가격 생성 (장중 5분봉)
        open_px = price
        intraday = _simulate_intraday(open_px, daily_vol)

        # VWAP 계산
        day_vwap = _calc_day_vwap(intraday)
        day_low  = min(c for _, c, _ in intraday)
        day_high = max(c for _, c, _ in intraday)
        close    = intraday[-1][1]

        # ATR5는 최근 5일 평균 True Range (초기값은 추정치)
        atr5_est = open_px * daily_vol * math.sqrt(5) * 0.8

        days.append({
            "date":     cur_date,
            "open":     open_px,
            "high":     day_high,
            "low":      day_low,
            "close":    close,
            "vwap":     day_vwap,
            "atr5_est": atr5_est,
            "intraday": intraday,  # [(time_str, price, volume), ...]
        })
        price = close

    return days


def _simulate_intraday(
    open_px: int, daily_vol: float
) -> List[tuple]:
    """장중 5분봉 (09:00~15:25, 총 77봉) 시뮬레이션."""
    candles = []
    price = open_px
    start = datetime.time(9, 0)
    total_vol = 0

    for i in range(77):  # 09:00 ~ 15:20 (5분 × 77봉)
        hour   = 9 + (i * 5) // 60
        minute = (i * 5) % 60
        t      = datetime.time(hour, minute)

        # 개장 초반 변동성 큰 거래량
        if i < 6:   vol_mult = 3.0
        elif i > 70: vol_mult = 2.0
        else:        vol_mult = 1.0

        vol = int(random.normalvariate(50000, 15000) * vol_mult)
        vol = max(1000, vol)

        # 가격 변동 (장중 랜덤워크)
        step = open_px * daily_vol / math.sqrt(77)
        price_change = random.normalvariate(0, step)

        # 트렌드 주입 (약한 상승 bias)
        trend = open_px * 0.0001
        price = max(int(price + price_change + trend), 100)

        candles.append((t, price, vol))

    return candles


def _calc_day_vwap(intraday: List[tuple]) -> float:
    cum_pv, cum_vol = 0.0, 0
    for _, price, vol in intraday:
        cum_pv  += price * vol
        cum_vol += vol
    return cum_pv / cum_vol if cum_vol > 0 else 0.0


def calc_atr5(days: List[Dict], idx: int) -> float:
    """최근 5일 True Range 평균."""
    if idx < 1:
        return days[idx]["atr5_est"]
    tr_list = []
    for j in range(max(0, idx - 4), idx + 1):
        prev_close = days[j-1]["close"] if j > 0 else days[j]["open"]
        tr = max(
            days[j]["high"] - days[j]["low"],
            abs(days[j]["high"] - prev_close),
            abs(days[j]["low"]  - prev_close),
        )
        tr_list.append(tr)
    return sum(tr_list) / len(tr_list)


# ==============================================================
# 백테스트 실행
# ==============================================================

@dataclass
class Trade:
    date:       datetime.date
    entry_time: datetime.time
    exit_time:  datetime.time
    entry_px:   int
    exit_px:    int
    qty:        int
    profit_pct: float
    reason:     str


def run_backtest(
    initial_budget: int   = 1_000_000,   # 1백만원 (AVWAP 전용 예산)
    n_days:         int   = 504,
    compounding:    bool  = True,         # 복리 재투자
    verbose:        bool  = False,
) -> Dict:
    """
    AVWAP 전략 백테스트.

    Parameters
    ----------
    initial_budget : 초기 AVWAP 전용 예산 (원)
    n_days         : 백테스트 기간 (거래일)
    compounding    : 복리 재투자 여부
    verbose        : 일별 상세 출력
    """
    days    = generate_krx_etf_days(n_days=n_days)
    budget  = initial_budget
    trades: List[Trade] = []
    year_pnl: Dict[int, int] = {}

    for day_idx, day in enumerate(days):
        atr5         = calc_atr5(days, day_idx)
        prev_vwap    = days[day_idx-1]["vwap"] if day_idx > 0 else 0.0
        intraday     = day["intraday"]
        day_date     = day["date"]
        year         = day_date.year

        day_locked   = False
        position_qty = 0
        position_avg = 0
        last_exit_t  = None
        day_pnl      = 0

        cum_pv  = 0.0
        cum_vol = 0
        vwap_5m_hist = []
        day_low  = 999999999

        for bar_idx, (bar_time, cur_price, volume) in enumerate(intraday):
            # 고/저가 갱신
            day_low = min(day_low, cur_price)

            # VWAP 누적
            cum_pv  += cur_price * volume
            cum_vol += volume
            intraday_vwap = calc_vwap(cum_pv, cum_vol)
            vwap_5m_hist.append(intraday_vwap)
            vwap_5m_avg = calc_5m_avg_vwap(vwap_5m_hist)

            # 타임쉴드
            if bar_time < TIME_SHIELD_END:
                continue

            # 강제 청산
            if bar_time >= TIME_FORCE_EXIT:
                if position_qty > 0:
                    fee     = cur_price * position_qty * COMMISSION_RATE
                    profit  = (cur_price - position_avg) * position_qty - int(fee * 2)
                    pct     = (cur_price - position_avg) / position_avg * 100
                    day_pnl += profit
                    if compounding:
                        budget += profit
                    trades.append(Trade(
                        day_date, bar_time, bar_time,
                        position_avg, cur_price, position_qty,
                        pct, "타임스탑"
                    ))
                    position_qty = 0
                break

            if day_locked:
                continue

            remaining_pct  = calc_remaining_energy(cur_price, day_low, atr5)
            dyn_target_pct = calc_dynamic_target(remaining_pct)

            # ── 포지션 청산 체크 ─────────────────────────────────
            if position_qty > 0:
                profit_pct = (cur_price - position_avg) / position_avg * 100
                target_px  = round(position_avg * (1 + dyn_target_pct / 100))
                stop_px    = round(position_avg * (1 + HARD_STOP_PCT / 100))

                if cur_price >= target_px:
                    fee    = cur_price * position_qty * COMMISSION_RATE
                    profit = (cur_price - position_avg) * position_qty - int(fee * 2)
                    day_pnl += profit
                    if compounding:
                        budget += profit
                    trades.append(Trade(
                        day_date, intraday[0][0], bar_time,
                        position_avg, cur_price, position_qty,
                        profit_pct, "익절"
                    ))
                    position_qty = 0
                    last_exit_t  = bar_time
                    continue

                if cur_price <= stop_px:
                    fee    = cur_price * position_qty * COMMISSION_RATE
                    profit = (cur_price - position_avg) * position_qty - int(fee * 2)
                    day_pnl += profit
                    if compounding:
                        budget += profit
                    trades.append(Trade(
                        day_date, intraday[0][0], bar_time,
                        position_avg, cur_price, position_qty,
                        profit_pct, "하드스탑"
                    ))
                    position_qty = 0
                    day_locked   = True
                    continue

            # ── 진입 조건 체크 ───────────────────────────────────
            if bar_time >= TIME_NO_ENTRY:
                continue

            # 쿨다운
            if last_exit_t:
                if (
                    datetime.datetime.combine(day_date, bar_time) -
                    datetime.datetime.combine(day_date, last_exit_t)
                ).seconds / 60 < COOLDOWN_MINUTES:
                    continue

            can_enter, _ = check_entry_signal(
                intraday_vwap, prev_vwap, vwap_5m_avg, remaining_pct
            )
            if can_enter and position_qty == 0:
                qty = max(1, math.floor(budget / cur_price))
                if qty > 0:
                    position_qty = qty
                    position_avg = cur_price

        year_pnl[year] = year_pnl.get(year, 0) + day_pnl
        if verbose and day_pnl != 0:
            print(f"  {day_date} | {day_pnl:+,}원 | 누적: {budget:,}원")

    # ── 결과 분석 ─────────────────────────────────────────────
    wins   = [t for t in trades if t.profit_pct >= 0]
    losses = [t for t in trades if t.profit_pct <  0]
    hard_stops = [t for t in trades if t.reason == "하드스탑"]
    total_pnl  = budget - initial_budget

    result = {
        "initial_budget":   initial_budget,
        "final_budget":     budget,
        "total_pnl":        total_pnl,
        "total_return_pct": total_pnl / initial_budget * 100,
        "total_trades":     len(trades),
        "wins":             len(wins),
        "losses":           len(losses),
        "hard_stops":       len(hard_stops),
        "win_rate":         len(wins) / len(trades) * 100 if trades else 0,
        "avg_win_pct":      sum(t.profit_pct for t in wins)  / len(wins)  if wins  else 0,
        "avg_loss_pct":     sum(t.profit_pct for t in losses)/ len(losses)if losses else 0,
        "year_pnl":         year_pnl,
        "trades":           trades,
        "n_days":           n_days,
    }
    return result


def print_report(r: Dict):
    """백테스트 결과 출력."""
    print("=" * 55)
    print(" AVWAP 퀀트 엔진 백테스트 결과")
    print(" KODEX 반도체TOP10레버리지 (488080) 기반 시뮬레이션")
    print("=" * 55)
    print(f" 백테스트 기간:  {r['n_days']}거래일 (~2년)")
    print(f" 초기 예산:      {r['initial_budget']:>12,}원")
    print(f" 최종 잔고:      {r['final_budget']:>12,}원")
    sign = "+" if r['total_pnl'] >= 0 else ""
    print(f" 총 손익:     {sign}{r['total_pnl']:>12,}원 ({sign}{r['total_return_pct']:.2f}%)")
    print("-" * 55)
    print(f" 총 출장:        {r['total_trades']:>4}회")
    print(f" 승률:           {r['win_rate']:.1f}%  ({r['wins']}승 {r['losses']}패)")
    print(f" 하드스탑:       {r['hard_stops']:>4}회")
    print(f" 평균 익절폭:    +{r['avg_win_pct']:.2f}%")
    print(f" 평균 손절폭:    {r['avg_loss_pct']:.2f}%")
    print("-" * 55)
    print(" 연도별 손익:")
    for year, pnl in sorted(r["year_pnl"].items()):
        sign = "+" if pnl >= 0 else ""
        print(f"   {year}년: {sign}{pnl:,}원")
    print("=" * 55)

    # 상세 내역 (최근 20건)
    print("\n 최근 20건 거래:")
    print(f"  {'날짜':<12} {'진입':>8} {'청산':>8} {'수익률':>8} {'사유'}")
    print(f"  {'-'*52}")
    for t in r["trades"][-20:]:
        sign = "+" if t.profit_pct >= 0 else ""
        print(f"  {str(t.date):<12} {t.entry_px:>8,} {t.exit_px:>8,} "
              f"{sign}{t.profit_pct:>6.2f}%  {t.reason}")


if __name__ == "__main__":
    print("\n[1] 단리 모드 (1백만원 고정 예산)")
    r1 = run_backtest(initial_budget=1_000_000, compounding=False)
    print_report(r1)

    print("\n[2] 복리 모드 (100% 재투자)")
    r2 = run_backtest(initial_budget=1_000_000, compounding=True)
    print_report(r2)

    print("\n[3] 3백만원 복리")
    r3 = run_backtest(initial_budget=3_000_000, compounding=True)
    print_report(r3)
