# ==============================================================
# [notifier.py] 텔레그램 알림 모듈 v5.0
#
# 무한매매 + AVWAP 이벤트 알림 완전 구현
# - notify_infinite_buy()  : 무한매매 매수 상세 알림
# - notify_infinite_sell() : 무한매매 매도 상세 알림
# - notify_avwap_entry()   : AVWAP 진입 알림
# - notify_avwap_exit()    : AVWAP 청산 알림
# - notify_avwap_locked()  : AVWAP 하드스탑 당일동결 알림
# ==============================================================
import logging
import asyncio
import datetime
import requests
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


class Notifier:
    """텔레그램 Bot API 래퍼 — 매매 알림 전송 전용."""

    BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: int):
        self._token   = token
        self._chat_id = chat_id
        self._url     = self.BASE_URL.format(token=token)

    def _now_str(self) -> str:
        return datetime.datetime.now(KST).strftime("%H:%M:%S")

    @staticmethod
    def _fmt_krw(v: int) -> str:
        return f"{int(v):,}원"

    @staticmethod
    def _fmt_pct(v: float) -> str:
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.2f}%"

    # ----------------------------------------------------------
    # 기본 전송
    # ----------------------------------------------------------
    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        if not self._token or not self._chat_id:
            log.warning("[Notifier] 토큰 또는 chat_id 미설정")
            return False
        try:
            resp = requests.post(
                self._url,
                json={"chat_id": self._chat_id, "text": text,
                      "parse_mode": parse_mode},
                timeout=10,
            )
            if not resp.ok:
                log.warning(f"[Notifier] 전송 실패: {resp.status_code}")
                return False
            return True
        except Exception as e:
            log.error(f"[Notifier] 전송 예외: {e}")
            return False

    async def send_async(self, text: str, parse_mode: str = "HTML") -> bool:
        return await asyncio.to_thread(self.send, text, parse_mode)

    # ==========================================================
    # 무한매매 알림
    # ==========================================================

    def notify_infinite_buy(
        self,
        code:        str,
        name:        str,
        qty:         int,
        price:       int,
        amount:      int,
        order_type:  str   = "동시호가",  # 동시호가 / 지정가 / 시장가
        desc:        str   = "",          # 평단매수 / 별값매수 / 줍줍(N) 등
        t_val:       float = 0.0,
        split:       int   = 0,
        phase:       str   = "",          # 전반전 / 후반전 / 새출발
        avg_price:   int   = 0,           # 매수 후 새 평단
        total_qty:   int   = 0,           # 매수 후 보유수량
        large_num:   int   = 0,           # 큰수
    ):
        phase_icon = {
            "전반전": "🌓", "후반전": "🌕",
            "새출발": "✨", "리버스": "🚨",
        }.get(phase, "📋")

        lines = [
            f"🔴 <b>무한매매 매수</b>  {name}",
            f"종목: {code}  [{order_type}]",
            f"수량: <b>{qty:,}주</b>  단가: <b>{self._fmt_krw(price)}</b>",
            f"금액: {self._fmt_krw(amount)}",
        ]
        if desc:
            lines.append(f"주문: {desc}")
        if t_val > 0 or phase:
            t_str = f"{t_val:.2f}T/{split}분할" if split > 0 else f"{t_val:.2f}T"
            lines.append(f"진행: {phase_icon} {t_str}  {phase}")
        if avg_price > 0:
            lines.append(f"새 평단: {self._fmt_krw(avg_price)}  보유: {total_qty:,}주")
        if large_num > 0:
            lines.append(f"큰수: {self._fmt_krw(large_num)}")
        lines.append(f"🕐 {self._now_str()}")
        self.send("\n".join(lines))

    def notify_infinite_sell(
        self,
        code:        str,
        name:        str,
        qty:         int,
        price:       int,
        amount:      int,
        profit:      int,
        profit_pct:  float,
        sell_type:   str  = "별값매도",   # 별값매도(1/4) / 목표가매도(전량) / 리버스매도
        reason:      str  = "",
        remain_qty:  int  = 0,
        avg_price:   int  = 0,
    ):
        icon = "🟢" if profit >= 0 else "🔴"
        sign = "+" if profit >= 0 else ""

        lines = [
            f"🔵 <b>무한매매 매도</b>  {name}  {icon}",
            f"종목: {code}  [{sell_type}]",
            f"수량: <b>{qty:,}주</b>  단가: <b>{self._fmt_krw(price)}</b>",
            f"금액: {self._fmt_krw(amount)}",
            f"실현손익: <b>{sign}{self._fmt_krw(profit)}</b>  ({sign}{profit_pct:.2f}%)",
        ]
        if reason:
            lines.append(f"사유: {reason}")
        if remain_qty > 0:
            lines.append(f"잔량: {remain_qty:,}주  평단: {self._fmt_krw(avg_price)}")
        elif remain_qty == 0:
            lines.append("잔량: 0주 — 사이클 완료 🎉")
        lines.append(f"🕐 {self._now_str()}")
        self.send("\n".join(lines))

    # ==========================================================
    # AVWAP 알림
    # ==========================================================

    def notify_avwap_entry(
        self,
        code:            str,
        name:            str,
        qty:             int,
        price:           int,
        amount:          int,
        target_pct:      float,
        hard_stop_pct:   float,
        remaining_pct:   float,
        intraday_vwap:   float,
        prev_day_vwap:   float,
        today_trades:    int,
    ):
        target_px    = round(price * (1 + target_pct / 100))
        stop_px      = round(price * (1 + hard_stop_pct / 100))
        vwap_dir     = "🔺" if intraday_vwap > prev_day_vwap else "🔻"

        lines = [
            f"⚡️ <b>AVWAP 진입</b>  {name}",
            f"종목: {code}  [장중 단타]",
            f"매수: <b>{qty:,}주</b> @ <b>{self._fmt_krw(price)}</b>",
            f"금액: {self._fmt_krw(amount)}",
            f"",
            f"🎯 목표가: <b>{self._fmt_krw(target_px)}</b>  (+{target_pct:.1f}%)",
            f"🛑 하드스탑: {self._fmt_krw(stop_px)}  ({hard_stop_pct:.1f}%)",
            f"💪 잔여체력: <b>{remaining_pct:.1f}%</b>",
            f"",
            f"당일VWAP: {intraday_vwap:,.0f}원  {vwap_dir}  전일: {prev_day_vwap:,.0f}원",
            f"오늘 {today_trades}차 출장",
            f"🕐 {self._now_str()}",
        ]
        self.send("\n".join(lines))

    def notify_avwap_exit(
        self,
        code:         str,
        name:         str,
        qty:          int,
        price:        int,
        profit:       int,
        profit_pct:   float,
        reason:       str,    # 익절 / 하드스탑 / 타임스탑
        today_pnl:    int,
        today_trades: int,
    ):
        if reason == "익절":
            icon, title = "✅", "AVWAP 익절"
        elif reason == "하드스탑":
            icon, title = "🩸", "AVWAP 하드스탑"
        else:
            icon, title = "🔔", f"AVWAP {reason}"

        sign = "+" if profit >= 0 else ""
        day_sign = "+" if today_pnl >= 0 else ""

        lines = [
            f"{icon} <b>{title}</b>  {name}",
            f"종목: {code}",
            f"매도: <b>{qty:,}주</b> @ <b>{self._fmt_krw(price)}</b>",
            f"",
            f"실현손익: <b>{sign}{self._fmt_krw(profit)}</b>  ({sign}{profit_pct:.2f}%)",
            f"당일 누적: {day_sign}{self._fmt_krw(today_pnl)}  (총 {today_trades}회 출장)",
            f"🕐 {self._now_str()}",
        ]
        self.send("\n".join(lines))

    def notify_avwap_locked(
        self,
        code:  str,
        name:  str,
        price: int,
        loss:  int,
    ):
        self.send(
            f"🔒 <b>AVWAP 당일 매매 동결</b>  {name}\n"
            f"하드스탑 발동 @ {self._fmt_krw(price)}\n"
            f"손실: {self._fmt_krw(loss)}\n"
            f"오늘 남은 시간 동안 재진입 없음\n"
            f"🕐 {self._now_str()}"
        )

    # ==========================================================
    # 시스템 알림
    # ==========================================================

    def notify_error(self, context: str, error: str):
        self.send(
            f"⚠️ <b>오류 발생</b>\n"
            f"위치: {context}\n"
            f"내용: {error}\n"
            f"🕐 {self._now_str()}"
        )

    def notify_low_balance(self, deposit: int, required: int):
        self.send(
            f"🚨 <b>예수금 부족 경고</b>\n"
            f"현재 예수금: {self._fmt_krw(deposit)}\n"
            f"필요 금액:   {self._fmt_krw(required)}\n"
            f"부족분:      {self._fmt_krw(required - deposit)}\n"
            f"⛔ 매매를 일시 중지합니다.\n"
            f"🕐 {self._now_str()}"
        )

    def notify_daily_report(
        self,
        deposit:      int,
        eval_total:   int,
        eval_profit:  int,
        profit_pct:   float,
        trades:       list,
        avwap_pnl:    int   = 0,
        avwap_trades: int   = 0,
    ):
        sign     = "+" if eval_profit >= 0 else ""
        icon     = "🟢" if eval_profit >= 0 else "🔴"
        a_sign   = "+" if avwap_pnl >= 0 else ""

        lines = [
            f"📊 <b>일일 정산 리포트</b>  {icon}",
            f"",
            f"💰 예수금:   {self._fmt_krw(deposit)}",
            f"📈 평가금액: {self._fmt_krw(eval_total)}",
            f"💹 평가손익: <b>{sign}{self._fmt_krw(eval_profit)}</b>  ({sign}{profit_pct:.2f}%)",
        ]
        if avwap_trades > 0:
            lines += [
                f"",
                f"⚡️ AVWAP 당일손익: <b>{a_sign}{self._fmt_krw(avwap_pnl)}</b>"
                f"  ({avwap_trades}회 출장)",
            ]
        if trades:
            lines += ["", f"📋 당일 체결: {len(trades)}건"]
        lines.append(f"🕐 {self._now_str()}")
        self.send("\n".join(lines))

    # ==========================================================
    # 레거시 호환 (기존 코드에서 호출하는 메서드 유지)
    # ==========================================================
    def notify_buy(self, code, name, qty, price, amount):
        """레거시 호환 — notify_infinite_buy 로 위임."""
        self.notify_infinite_buy(
            code=code, name=name, qty=qty,
            price=price, amount=amount,
        )

    def notify_sell(self, code, name, qty, price, amount, profit, profit_pct):
        """레거시 호환 — notify_infinite_sell 로 위임."""
        self.notify_infinite_sell(
            code=code, name=name, qty=qty,
            price=price, amount=amount,
            profit=profit, profit_pct=profit_pct,
        )
