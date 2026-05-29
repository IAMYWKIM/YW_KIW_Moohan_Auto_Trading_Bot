# ==============================================================
# [main.py] 국내 ETF 무한매매 + AVWAP 봇 v5.0
# ==============================================================
import os
import logging
import asyncio
import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from logger_setup import setup_logger
setup_logger()
log = logging.getLogger("main")

load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
TRADE_MODE       = os.getenv("TRADE_MODE", "MOCK").upper()
KST              = ZoneInfo("Asia/Seoul")


def _check_env():
    required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    if TRADE_MODE == "REAL":
        required += ["KIWOOM_APP_KEY", "KIWOOM_SECRET_KEY", "KIWOOM_ACCOUNT_NO"]
    else:
        required += ["KIWOOM_APP_KEY_MOCK", "KIWOOM_SECRET_KEY_MOCK",
                     "KIWOOM_ACCOUNT_NO_MOCK"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        log.critical(f"[Main] ❌ 필수 환경 변수 누락: {missing}")
        raise SystemExit(1)


try:
    from telegram.ext import Application
    from telegram_bot import TelegramController
except ImportError as e:
    log.critical(f"[Main] ❌ python-telegram-bot 임포트 실패: {e}")
    raise SystemExit(1)

from kiwoom_api      import KiwoomBroker
from database        import Database
from notifier        import Notifier
from market_calendar import MarketCalendar

try:
    from trading_engine import TradingEngine
    def _make_engine(broker, db, notifier, calendar):
        return TradingEngine(broker, db, notifier, calendar)
except Exception as e:
    log.warning(f"[Main] trading_engine 임포트 실패 ({e})")
    class _DummyEngine:
        async def check_and_sell_profit(self): pass
        async def loc_buy(self): pass
        async def auction_buy(self): pass
        async def vrev_rebalance(self, symbols): pass
        def build_sync_plan(self, sym, cur, pos): return {
            "phase":"?","t_val":0,"star_ratio":0,
            "star_price":0,"target_price":0,"large_num":0,
            "plan":{"buy":[],"sell":[]}}
    def _make_engine(broker, db, notifier, calendar):
        return _DummyEngine()

# ── AVWAP 엔진 ──────────────────────────────────────────────
try:
    from avwap_engine import AVWAPEngine
    AVWAP_AVAILABLE = True
except ImportError:
    log.warning("[Main] avwap_engine.py 없음 — AVWAP 비활성")
    AVWAP_AVAILABLE = False

try:
    from scheduler_core  import CoreScheduler
    from scheduler_trade import TradeScheduler
    SCHED_CLASS = True
except Exception as e:
    log.warning(f"[Main] 스케줄러 임포트 실패 ({e})")
    SCHED_CLASS = False


# ==============================================================
# AVWAP 심볼 초기화 (config에서 avwap_budget > 0 인 종목)
# ==============================================================
def _init_avwap(broker, db, notifier) -> "AVWAPEngine | None":
    if not AVWAP_AVAILABLE:
        return None

    symbols = broker.cfg.get("SYMBOLS", [])
    avwap_syms = [
        s for s in symbols
        if s.get("active", True) and s.get("avwap_budget", 0) > 0
    ]
    if not avwap_syms:
        log.info("[Main] AVWAP 활성 종목 없음 (avwap_budget 설정 필요)")
        return None

    engine = AVWAPEngine(broker, db, notifier)
    engine.init_symbols(avwap_syms)
    log.info(
        f"[Main] AVWAP 엔진 초기화 완료 — "
        f"{len(avwap_syms)}종목: "
        f"{[s['name'] for s in avwap_syms]}"
    )
    return engine


# ==============================================================
# Job 등록
# ==============================================================
def register_jobs(app, broker, db, notifier, engine, calendar, avwap_engine):
    if not SCHED_CLASS:
        return
    jq   = app.job_queue
    data = app.bot_data

    core_sched  = CoreScheduler(broker, db, notifier)
    trade_sched = TradeScheduler(broker, db, notifier, engine, calendar)
    data["core_sched"]   = core_sched
    data["trade_sched"]  = trade_sched
    data["avwap_engine"] = avwap_engine

    # ── 코어 스케줄 ────────────────────────────────────────────
    jq.run_daily(core_sched.morning_check,
                 time=datetime.time(8, 50, tzinfo=KST),
                 days=(0,1,2,3,4), chat_id=TELEGRAM_CHAT_ID, data=data)
    jq.run_daily(core_sched.daily_settlement,
                 time=datetime.time(15, 35, tzinfo=KST),
                 days=(0,1,2,3,4), chat_id=TELEGRAM_CHAT_ID, data=data)
    jq.run_daily(core_sched.midnight_reset,
                 time=datetime.time(0, 5, tzinfo=KST),
                 days=tuple(range(7)), chat_id=TELEGRAM_CHAT_ID, data=data)
    jq.run_repeating(core_sched.token_refresh,
                     interval=6*3600, first=60,
                     chat_id=TELEGRAM_CHAT_ID, data=data)

    # ── 무한매매 스케줄 ─────────────────────────────────────────
    jq.run_repeating(trade_sched.profit_monitor,
                     interval=60, first=30,
                     chat_id=TELEGRAM_CHAT_ID, data=data)
    jq.run_daily(trade_sched.loc_buy_start,
                 time=datetime.time(15, 10, tzinfo=KST),
                 days=(0,1,2,3,4), chat_id=TELEGRAM_CHAT_ID, data=data)
    # V-REV LOC: 승승장군 원본 알고리즘 — 15:10 동시호가 SMA5 기준
    jq.run_daily(trade_sched.vrev_loc_start,
                 time=datetime.time(15, 10, tzinfo=KST),
                 days=(0,1,2,3,4), chat_id=TELEGRAM_CHAT_ID, data=data)
    jq.run_daily(trade_sched.auction_order,
                 time=datetime.time(15, 20, tzinfo=KST),
                 days=(0,1,2,3,4), chat_id=TELEGRAM_CHAT_ID, data=data)
    jq.run_daily(trade_sched.vrev_rebalance,
                 time=datetime.time(9, 10, tzinfo=KST),
                 days=(0,1,2,3,4), chat_id=TELEGRAM_CHAT_ID, data=data)

    # ── AVWAP 스케줄 ────────────────────────────────────────────
    if avwap_engine:
        # 매일 09:00 일일 초기화
        jq.run_daily(_avwap_morning_reset,
                     time=datetime.time(9, 0, tzinfo=KST),
                     days=(0,1,2,3,4),
                     chat_id=TELEGRAM_CHAT_ID, data=data)
        # 매 5분마다 틱 처리 (09:00 ~ 15:30)
        jq.run_repeating(_avwap_tick,
                         interval=300,   # 5분
                         first=60,
                         chat_id=TELEGRAM_CHAT_ID, data=data)
        log.info("[Main] AVWAP 스케줄 등록 완료 (09:00 초기화, 5분마다 틱)")

    log.info("[Main] 스케줄 Job 등록 완료")


# ==============================================================
# AVWAP Job 핸들러
# ==============================================================
async def _avwap_morning_reset(context):
    """09:00 — AVWAP 일일 초기화."""
    avwap_engine = context.bot_data.get("avwap_engine")
    if not avwap_engine:
        return
    broker = context.bot_data.get("broker")

    # 전일 VWAP 맵 수집 (가능하면)
    prev_vwap_map = {}
    if broker:
        try:
            for code in list(avwap_engine.states.keys()):
                info = broker.get_stock_info(code)
                if info.get("cur_price", 0) > 0:
                    prev_vwap_map[code] = info.get("prev_close", 0)
        except Exception:
            pass

        # ATR5 업데이트 (전일 OHLCV 기반 실제 계산)
        try:
            import math
            for code, st in avwap_engine.states.items():
                info     = broker.get_stock_info(code)
                cur      = info.get("cur_price", 0)
                prev_cls = info.get("prev_close", 0)
                day_h    = info.get("day_high", 0)
                day_l    = info.get("day_low",  0)
                if cur > 0 and prev_cls > 0:
                    # True Range = max(당일고-당일저, |당일고-전일종가|, |당일저-전일종가|)
                    # ATR5 추정 = 최근 5일 평균 TR (오늘 TR × 5일 근사)
                    tr_today = max(
                        day_h - day_l if day_h > 0 and day_l > 0 else 0,
                        abs(day_h - prev_cls) if day_h > 0 else 0,
                        abs(day_l - prev_cls) if day_l > 0 else 0,
                    )
                    # TR이 없으면 전일종가 × 일간변동성 추정 (2배 레버리지 기준 3%)
                    if tr_today <= 0:
                        tr_today = int(prev_cls * 0.03)
                    # ATR5 = 오늘 TR 가중 (실제는 5일 평균, 근사값)
                    atr5 = max(tr_today, int(cur * 0.015))
                    avwap_engine.update_atr5(code, float(atr5))
                    log.info(
                        f"[Main] {code} ATR5 업데이트: {atr5:,}원 "
                        f"(TR={tr_today:,} / Amp5={atr5/cur*100:.1f}%)"
                    )
        except Exception as e:
            log.warning(f"[Main] ATR5 업데이트 실패: {e}")

    avwap_engine.morning_reset(prev_vwap_map)
    log.info("[Main] AVWAP 일일 초기화 완료")


async def _avwap_tick(context):
    """5분마다 — AVWAP 틱 처리 (가격/거래량 갱신 + 진입/청산 판단)."""
    avwap_engine = context.bot_data.get("avwap_engine")
    broker       = context.bot_data.get("broker")
    if not avwap_engine or not broker:
        return

    for code, st in avwap_engine.states.items():
        try:
            info   = broker.get_stock_info(code)
            price  = info.get("cur_price", 0)
            volume = info.get("volume", 1000)   # 거래량 (API 지원 시)
            if price > 0:
                await avwap_engine.on_tick(code, price, volume)
        except Exception as e:
            log.debug(f"[Main] AVWAP 틱 실패 {code}: {e}")


# ==============================================================
# post_init
# ==============================================================
async def post_init(application: Application):
    tg_ctrl      = application.bot_data["tg_ctrl"]
    notifier     = application.bot_data["notifier"]
    broker       = application.bot_data["broker"]
    avwap_engine = application.bot_data.get("avwap_engine")

    await tg_ctrl.set_bot_commands(application)

    now = datetime.datetime.now(KST)
    try:
        balance = broker.get_balance()
        deposit = balance.get("deposit", 0)
        bal_str = f"💰 예수금: {deposit:,}원"
    except Exception:
        bal_str = "💰 예수금: API 연결 확인 필요"

    mode_str  = "🔴 실전" if TRADE_MODE == "REAL" else "🟡 모의투자"
    avwap_str = ""
    if avwap_engine:
        codes = list(avwap_engine.states.keys())
        avwap_str = f"\n⚡️ AVWAP 엔진: {len(codes)}종목 활성"

    await notifier.send_async(
        f"🚀 <b>국내 ETF 무한매매 봇 v5.0 시작</b>\n"
        f"\n"
        f"🕐 {now.strftime('%Y-%m-%d %H:%M:%S')} KST\n"
        f"💹 모드: {mode_str}\n"
        f"{bal_str}"
        f"{avwap_str}\n"
        f"\n"
        f"명령어 목록: /help"
    )
    log.info("[Main] post_init 완료")


# ==============================================================
# main
# ==============================================================
def main():
    _check_env()
    os.makedirs("data", exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    now = datetime.datetime.now(KST)
    log.info("=" * 60)
    log.info("  국내 ETF 무한매매 + AVWAP 봇 v5.0 시작")
    log.info(f"  {now.strftime('%Y-%m-%d %H:%M:%S')} KST")
    log.info(f"  운영 모드: {'실전' if TRADE_MODE == 'REAL' else '모의투자 (MOCK)'}")
    log.info("=" * 60)

    broker   = KiwoomBroker()
    db       = Database()
    notifier = Notifier(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
    calendar = MarketCalendar()
    engine   = _make_engine(broker, db, notifier, calendar)

    # ── AVWAP 엔진 초기화 ──────────────────────────────────
    avwap_engine = _init_avwap(broker, db, notifier)

    log.info("[Main] 키움 REST API 연결 확인 중...")
    api_ok = broker.ping()
    if not api_ok:
        log.warning("[Main] ⚠️  API 연결 실패 — 텔레그램 봇은 정상 시작됩니다")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    tg_ctrl = TelegramController(
        cfg           = broker.cfg,
        broker        = broker,
        db            = db,
        notifier      = notifier,
        trading_engine= engine,
        admin_chat_id = TELEGRAM_CHAT_ID,
    )
    tg_ctrl.register_handlers(app)

    # ── AVWAP 엔진을 텔레봇에 주입 ─────────────────────────
    if avwap_engine:
        tg_ctrl.set_avwap_engine(avwap_engine)

    app.bot_data.update({
        "broker":       broker,
        "db":           db,
        "notifier":     notifier,
        "engine":       engine,
        "tg_ctrl":      tg_ctrl,
        "avwap_engine": avwap_engine,
    })

    register_jobs(app, broker, db, notifier, engine, calendar, avwap_engine)
    app.post_init = post_init

    log.info("[Main] 텔레그램 폴링 시작 — 명령어 수신 대기 중")
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )
    log.info("[Main] 봇 종료 완료")


if __name__ == "__main__":
    main()
