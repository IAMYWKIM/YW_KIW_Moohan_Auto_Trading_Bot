# ==============================================================
# [scheduler_core.py] 시스템 코어 스케줄러 v3.0
# - 토큰 자동 갱신, 아침 예수금 점검, 일일 정산, 자정 리셋
# - 레퍼런스 아키텍처(승승장군) SRP 분리 원칙 계승
# ==============================================================
import logging
import datetime
from zoneinfo import ZoneInfo
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


class CoreScheduler:
    """시스템 생명 유지 전담 코어 스케줄러."""

    def __init__(self, broker, db, notifier):
        self.broker   = broker
        self.db       = db
        self.notifier = notifier

    # ----------------------------------------------------------
    # 토큰 자동 갱신 (6시간마다)
    # ----------------------------------------------------------
    async def token_refresh(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            self.broker._get_token(force=True)
            log.info("[CoreSched] 토큰 갱신 완료")
        except Exception as e:
            log.exception("[CoreSched] 토큰 갱신 실패")
            self.notifier.notify_error("token_refresh", str(e))

    # ----------------------------------------------------------
    # 아침 예수금 점검 (08:50 KST)
    # ----------------------------------------------------------
    async def morning_check(self, context: ContextTypes.DEFAULT_TYPE):
        log.info("[CoreSched] 아침 예수금 점검 시작")
        try:
            balance = self.broker.get_balance()
            deposit      = balance.get("deposit", 0)
            withdrawable = balance.get("withdrawable", 0)

            # 설정된 종목 하루 매수 한도 합산
            cfg = self.broker.cfg
            symbols = cfg.get("SYMBOLS", [])
            total_required = sum(
                s.get("daily_buy_limit_krw", s.get("allocation_krw", 0))
                for s in symbols
                if s.get("active", True)
            )

            now = datetime.datetime.now(KST)
            mode_str = "🔴 실전" if cfg.get("TRADE_MODE", "MOCK") == "REAL" else "🟡 모의투자"

            msg = (
                f"🌅 <b>장 시작 브리핑</b>\n"
                f"🕐 {now.strftime('%Y-%m-%d %H:%M')} KST\n"
                f"💹 모드: {mode_str}\n"
                f"💰 예수금: {deposit:,}원\n"
                f"💳 출금가능: {withdrawable:,}원\n"
                f"📋 오늘 매수 예상: {total_required:,}원\n"
            )

            # 예수금 부족 경고
            if withdrawable < total_required:
                shortage = total_required - withdrawable
                msg += (
                    f"\n🚨 <b>예수금 부족!</b>\n"
                    f"부족분: {shortage:,}원\n"
                    f"⛔ 매매 일시 중지"
                )
                cfg.set("BOT_PAUSED", True)
                log.warning(f"[CoreSched] 예수금 부족 — 매매 중지 ({withdrawable:,} < {total_required:,})")
            else:
                msg += "✅ 예수금 충분 — 정상 매매"

            self.notifier.send(msg)
            log.info("[CoreSched] 아침 점검 완료")
        except Exception as e:
            log.exception("[CoreSched] 아침 점검 실패")
            self.notifier.notify_error("morning_check", str(e))

    # ----------------------------------------------------------
    # 일일 정산 리포트 (15:35 KST)
    # ----------------------------------------------------------
    async def daily_settlement(self, context: ContextTypes.DEFAULT_TYPE):
        log.info("[CoreSched] 일일 정산 시작")
        try:
            balance = self.broker.get_balance()
            deposit     = balance.get("deposit", 0)
            eval_total  = balance.get("eval_total", 0)
            eval_profit = balance.get("eval_profit", 0)
            profit_pct  = balance.get("profit_pct", 0.0)

            today  = datetime.date.today().isoformat()
            trades = self.db.get_trades_by_date(today)

            self.notifier.notify_daily_report(
                deposit, eval_total, eval_profit, profit_pct, trades
            )
            log.info("[CoreSched] 일일 정산 완료")
        except Exception as e:
            log.exception("[CoreSched] 일일 정산 실패")
            self.notifier.notify_error("daily_settlement", str(e))

    # ----------------------------------------------------------
    # 자정 DB 리셋 (00:05 KST)
    # ----------------------------------------------------------
    async def midnight_reset(self, context: ContextTypes.DEFAULT_TYPE):
        log.info("[CoreSched] 자정 리셋 시작")
        try:
            # BOT_PAUSED 해제 (예수금 부족으로 중지됐을 경우 다음 날 재개)
            cfg = self.broker.cfg
            if cfg.get("BOT_PAUSED", False):
                cfg.set("BOT_PAUSED", False)
                log.info("[CoreSched] BOT_PAUSED 자동 해제")

            # 오래된 로그/데이터 정리 (30일 이상)
            self.db.cleanup_old_trades(days=30)
            log.info("[CoreSched] 자정 리셋 완료")
        except Exception as e:
            log.exception("[CoreSched] 자정 리셋 실패")
            self.notifier.notify_error("midnight_reset", str(e))
