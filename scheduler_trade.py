# ==============================================================
# [scheduler_trade.py] 실전 매매 스케줄러 v3.0
# - 익절 감시 (60초), LOC 대안 분할매수 (15:10), 동시호가 (15:20)
# - V-REV 리밸런싱 (09:10)
# - 레퍼런스 아키텍처(승승장군) SRP 분리 원칙 계승
# ==============================================================
import logging
import datetime
from zoneinfo import ZoneInfo
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


class TradeScheduler:
    """실전 전투 매매 전담 스케줄러."""

    def __init__(self, broker, db, notifier, engine, calendar):
        self.broker   = broker
        self.db       = db
        self.notifier = notifier
        self.engine   = engine
        self.calendar = calendar

    def _is_trading_paused(self) -> bool:
        """BOT_PAUSED 플래그 확인."""
        return self.broker.cfg.get("BOT_PAUSED", False)

    def _is_market_open(self) -> bool:
        """현재 정규장 시간인지 확인."""
        now = datetime.datetime.now(KST)
        if now.weekday() >= 5:
            return False
        cfg = self.broker.cfg
        start_t = datetime.time(*map(int, cfg.get("START_TIME", "09:00").split(":")))
        end_t   = datetime.time(*map(int, cfg.get("END_TIME",   "15:20").split(":")))
        return start_t <= now.time() <= end_t

    # ----------------------------------------------------------
    # 익절 감시 (60초마다)
    # ----------------------------------------------------------
    async def profit_monitor(self, context: ContextTypes.DEFAULT_TYPE):
        if self._is_trading_paused():
            return
        if not self._is_market_open():
            return
        if not self.calendar.is_trading_day():
            return

        try:
            await self.engine.check_and_sell_profit()
        except Exception as e:
            log.exception("[TradeSched] 익절 감시 실패")
            self.notifier.notify_error("profit_monitor", str(e))

    # ----------------------------------------------------------
    # LOC 대안 — 15:10~15:20 분할 매수 시작
    # ----------------------------------------------------------
    async def loc_buy_start(self, context: ContextTypes.DEFAULT_TYPE):
        if self._is_trading_paused():
            log.info("[TradeSched] BOT_PAUSED — LOC 매수 건너뜀")
            return
        if not self.calendar.is_trading_day():
            log.info("[TradeSched] 휴장일 — LOC 매수 건너뜀")
            return

        log.info("[TradeSched] LOC 대안 분할 매수 시작 (15:10)")
        try:
            await self.engine.loc_buy()
        except Exception as e:
            log.exception("[TradeSched] LOC 매수 실패")
            self.notifier.notify_error("loc_buy_start", str(e))

    # ----------------------------------------------------------
    # 동시호가 잔여 주문 (15:20)
    # ----------------------------------------------------------
    async def auction_order(self, context: ContextTypes.DEFAULT_TYPE):
        if self._is_trading_paused():
            log.info("[TradeSched] BOT_PAUSED — 동시호가 건너뜀")
            return
        if not self.calendar.is_trading_day():
            return

        log.info("[TradeSched] 장마감 동시호가 주문 (15:20)")
        try:
            await self.engine.auction_buy()
        except Exception as e:
            log.exception("[TradeSched] 동시호가 주문 실패")
            self.notifier.notify_error("auction_order", str(e))

    # ----------------------------------------------------------
    # V-REV 리밸런싱 (09:10)
    # ----------------------------------------------------------
    async def vrev_rebalance(self, context: ContextTypes.DEFAULT_TYPE):
        if self._is_trading_paused():
            return
        if not self.calendar.is_trading_day():
            return

        cfg = self.broker.cfg
        symbols = cfg.get("SYMBOLS", [])
        vrev_symbols = [s for s in symbols if s.get("mode") == "VREV" and s.get("active", True)]
        if not vrev_symbols:
            return

        log.info(f"[TradeSched] V-REV 리밸런싱 시작 — {len(vrev_symbols)}종목")
        try:
            await self.engine.vrev_rebalance(vrev_symbols)
        except Exception as e:
            log.exception("[TradeSched] V-REV 리밸런싱 실패")
            self.notifier.notify_error("vrev_rebalance", str(e))
