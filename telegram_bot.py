# ==============================================================
# [telegram_bot.py] 국내 ETF 무한매매 봇 v4.0
# 승승장군 봇 UI/UX 완전 구현 (국내 ETF / KRW 버전)
#
# 구현 명령어:
#   /start      - 봇 정보 + 스케줄 + 명령어 목록
#   /sync       - 통합 지시서 (T값, 별값, 주문계획 per 종목)
#   /record     - 장부 조회 (일자별 매매 내역)
#   /settlement - 설정 현황 (분할/목표/밴드 인라인 버튼)
#   /ticker     - 종목 관리 (활성화/비활성화/추가/제거)
#   /mode       - INFINITE / VREV 모드 전환
#   /seed       - 시드머니(할당금) 설정
#   /balance    - 예수금 및 계좌 잔고
#   /holdings   - 보유 종목 평가손익
#   /report     - 당일 정산 리포트
#   /pause      - 매매 일시 중지
#   /resume     - 매매 재개
#   /cancel     - 미체결 주문 전량 취소
#   /help       - 명령어 도움말
#
# 기본 종목:
#   122630  KODEX 레버리지 (코스피 2배)
#   233740  KODEX 코스닥150레버리지 (코스닥 2배)
#   488080  KODEX 반도체TOP10레버리지 (반도체 2배)
# ==============================================================
import asyncio
import logging
import math
import datetime
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ContextTypes, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters,
)
# 무한매매 계산 엔진 (trading_engine.py 공용 함수)
from trading_engine import (
    calc_t_val, calc_star_ratio, calc_star_price,
    calc_target_price, calc_one_portion_qty, plan_loc_buy,
)
from kiwoom_api import round_to_tick

# ── 버전 정보 (version.py 없어도 동작) ─────────────────────
VERSION_TAG = "v5.0"
def get_telegram_version_text(active_symbols=None): return None
HISTORY = []
try:
    from version import VERSION_TAG, get_telegram_version_text, HISTORY
except Exception:
    pass  # version.py 없으면 기본값 사용

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
# 기본 종목 목록
DEFAULT_SYMBOLS = [
    {
        "code": "122630", "name": "KODEX 레버리지",
        "mode": "INFINITE", "active": True,
        "allocation_krw": 3_000_000, "split_count": 10,
        "target_profit_pct": 5.0, "vrev_band_pct": 3.0,
        "daily_buy_limit_krw": 300_000,
    },
    {
        "code": "233740", "name": "KODEX 코스닥150레버리지",
        "mode": "INFINITE", "active": True,
        "allocation_krw": 3_000_000, "split_count": 10,
        "target_profit_pct": 5.0, "vrev_band_pct": 3.0,
        "daily_buy_limit_krw": 300_000,
    },
    {
        "code": "488080", "name": "KODEX 반도체TOP10레버리지",
        "mode": "INFINITE", "active": False,
        "allocation_krw": 3_000_000, "split_count": 10,
        "target_profit_pct": 5.0, "vrev_band_pct": 3.0,
        "daily_buy_limit_krw": 300_000,
    },
]
# ConversationHandler 상태
(
    STATE_TICKER_ADD_CODE, STATE_TICKER_ADD_NAME,
    STATE_TICKER_ADD_ALLOC, STATE_SET_VALUE,
) = range(4)
SETTING_KEY_MAP = {
    "split":  ("split_count",         int,   "분할 횟수 (예: 10)",          "회"),
    "target": ("target_profit_pct",   float, "목표 수익률 (예: 5.0)",        "%"),
    "band":   ("vrev_band_pct",       float, "V-REV 밴드폭 (예: 3.0)",      "%"),
    "alloc":  ("allocation_krw",      int,   "할당금액 (예: 3000000)",       "원"),
    "limit":  ("daily_buy_limit_krw", int,   "일일 매수한도 (예: 300000)",   "원"),
    "avwap":  ("avwap_budget",        int,   "AVWAP 예산 (예: 1000000)",     "원"),
}
# ==============================================================
# TelegramController
# ==============================================================
class TelegramController:
    VERSION = VERSION_TAG
    def __init__(self, cfg, broker, db, notifier, trading_engine, admin_chat_id: int):
        self.cfg      = cfg
        self.broker   = broker
        self.db       = db
        self.notifier = notifier
        self.engine   = trading_engine
        self.admin_id = admin_chat_id
        self._pending = {}   # 설정 입력 대기 상태
    # ----------------------------------------------------------
    # 보안 게이트
    # ----------------------------------------------------------
    def _is_admin(self, update: Update) -> bool:
        uid = update.effective_chat.id
        if uid != self.admin_id:
            log.warning(f"[TG] 비인가 접근 차단: {uid}")
            return False
        return True
    # ----------------------------------------------------------
    # 핸들러 등록
    # ----------------------------------------------------------
    def register_handlers(self, app):
        cmds = [
            ("start",      self.cmd_start),
            ("sync",       self.cmd_sync),
            ("record",     self.cmd_record),
            ("settlement", self.cmd_settlement),
            ("ticker",     self.cmd_ticker),
            ("mode",       self.cmd_mode),
            ("seed",       self.cmd_seed),
            ("balance",    self.cmd_balance),
            ("holdings",   self.cmd_holdings),
            ("report",     self.cmd_report),
            ("pause",      self.cmd_pause),
            ("resume",     self.cmd_resume),
            ("cancel",     self.cmd_cancel),
            ("help",       self.cmd_help),
            ("buy",        self.cmd_buy),
            ("sell",       self.cmd_sell),
            ("sync_db",    self.cmd_sync_db),
            ("avwap",      self.cmd_avwap),
            ("version",    self.cmd_version),
            ("history",    self.cmd_history),
        ]
        for name, handler in cmds:
            app.add_handler(CommandHandler(name, handler))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, self.handle_message
        ))
        log.info(f"[TG] 핸들러 {len(cmds)}개 등록 완료")
    async def set_bot_commands(self, app):
        await app.bot.set_my_commands([
            BotCommand("start",      "봇 시작 및 스케줄 안내"),
            BotCommand("sync",       "통합 지시서 (T값·별값·주문계획)"),
            BotCommand("record",     "장부 조회 (일자별 매매 내역)"),
            BotCommand("settlement", "설정 현황 및 파라미터 변경"),
            BotCommand("ticker",     "종목 관리 (활성화/추가/제거)"),
            BotCommand("mode",       "INFINITE / V-REV 모드 전환"),
            BotCommand("seed",       "시드머니(할당금) 설정"),
            BotCommand("balance",    "예수금 및 계좌 잔고"),
            BotCommand("holdings",   "보유 종목 평가손익"),
            BotCommand("report",     "당일 정산 리포트"),
            BotCommand("pause",      "매매 일시 중지"),
            BotCommand("resume",     "매매 재개"),
            BotCommand("cancel",     "미체결 주문 전량 취소"),
            BotCommand("help",       "명령어 도움말"),
            BotCommand("buy",       "수동 매수 주문"),
            BotCommand("sell",      "수동 매도 주문"),
            BotCommand("sync_db",   "키움 잔고 → DB 동기화"),
            BotCommand("avwap",     "AVWAP 퀀트 엔진 현황 및 설정"),
            BotCommand("version",   "버전 정보 및 업데이트 내역"),
            BotCommand("history",   "졸업 명예의 전당 (완료 사이클)"),
        ])
    # ----------------------------------------------------------
    # 공용 헬퍼
    # ----------------------------------------------------------
    def _get_symbols(self) -> list:
        syms = self.cfg.get("SYMBOLS", [])
        if not syms:
            # 최초 실행: 기본 종목 저장
            self.cfg.set("SYMBOLS", DEFAULT_SYMBOLS)
            return DEFAULT_SYMBOLS
        return syms
    def _save_symbols(self, syms: list):
        self.cfg.set("SYMBOLS", syms)
    def _get_symbol(self, code: str) -> dict | None:
        return next((s for s in self._get_symbols() if s["code"] == code), None)
    def _get_active_symbols(self) -> list:
        return [s for s in self._get_symbols() if s.get("active", True)]
    def _market_status(self) -> str:
        now = datetime.datetime.now(KST)
        if now.weekday() >= 5:
            return "⛔ 주말 휴장"
        cfg  = self.cfg
        s_t  = datetime.time(*map(int, cfg.get("START_TIME", "09:00").split(":")))
        e_t  = datetime.time(*map(int, cfg.get("END_TIME",   "15:20").split(":")))
        t    = now.time()
        if t < s_t:
            return "🌅 장 전"
        if t <= e_t:
            return "🟢 정규장"
        if t <= datetime.time(15, 30):
            return "🔔 동시호가"
        return "🌙 장 마감"
    def _fmt_krw(self, v: int) -> str:
        return f"{v:,}원"
    def _fmt_pct(self, v: float) -> str:
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.2f}%"
    # ----------------------------------------------------------
    # /start
    # ----------------------------------------------------------
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        now      = datetime.datetime.now(KST)
        mode_str = "🔴 실전" if self.cfg.get("TRADE_MODE", "MOCK") == "REAL" else "🟡 모의투자"
        syms     = self._get_active_symbols()
        sym_list = "\n".join(
            f"  {'✅' if s.get('active') else '🔇'} {s['name']}({s['code']}) "
            f"[{'무한매매' if s.get('mode','INFINITE')=='INFINITE' else 'V-REV'}]"
            for s in self._get_symbols()
        )
        msg = (
            f"📊 <b>국내 ETF 무한매매 봇 {self.VERSION}</b>\n"
            f"💹 {mode_str} | {self._market_status()}\n"
            f"🕐 {now.strftime('%Y-%m-%d %H:%M')} KST\n\n"
            f"⏰ <b>[ 운영 스케줄 ]</b>\n"
            f"◆ 08:50 : 💰 예수금 점검 및 장 시작 알림\n"
            f"◆ 09:10 : 🔄 V-REV 리밸런싱 (해당 종목)\n"
            f"◆ 15:10 : 🔴 LOC 대안 분할 매수 시작\n"
            f"◆ 15:20 : 🔔 동시호가 잔여 주문\n"
            f"◆ 15:35 : 📋 일일 정산 리포트\n"
            f"◆ 매 1분 : 👁 익절 감시 (장중)\n"
            f"◆ 6시간 : 🔑 API 토큰 자동 갱신\n\n"
            f"🔧 <b>[ 주요 명령어 ]</b>\n"
            f"▶ /sync       : 📋 통합 지시서 조회\n"
            f"▶ /record     : 📊 장부 동기화 및 조회\n"
            f"▶ /settlement : ⚙️ 설정 현황/파라미터 변경\n"
            f"▶ /ticker     : 🔄 종목 관리 (추가/제거)\n"
            f"▶ /mode       : 🎯 INFINITE/V-REV 전환\n"
            f"▶ /seed       : 💵 시드머니 관리\n"
            f"▶ /balance    : 💰 예수금 조회\n"
            f"▶ /holdings   : 📈 보유 종목 현황\n"
            f"▶ /report     : 📋 당일 정산\n"
            f"▶ /pause /resume : ⏸ 매매 중지/재개\n\n"
            f"⚠️ /cancel : 🔒 미체결 전량 취소\n\n"
            f"<b>[ 운용 종목 ]</b>\n{sym_list}"
        )
        keyboard = [
            [
                InlineKeyboardButton("📋 통합 지시서", callback_data="CMD:sync"),
                InlineKeyboardButton("📊 장부 조회",   callback_data="CMD:record"),
            ],
            [
                InlineKeyboardButton("⚡️ AVWAP 현황",  callback_data="AVWAP:REFRESH"),
                InlineKeyboardButton("🏆 졸업 전당",    callback_data="CMD:history"),
            ],
            [
                InlineKeyboardButton("🔴 수동 매수",   callback_data="BUY:START"),
                InlineKeyboardButton("🔵 수동 매도",   callback_data="SEL:START"),
            ],
            [
                InlineKeyboardButton("⚙️ 설정 현황",   callback_data="CMD:settlement"),
                InlineKeyboardButton("🔄 종목 관리",   callback_data="CMD:ticker"),
            ],
            [
                InlineKeyboardButton("💰 잔고 조회",   callback_data="CMD:balance"),
                InlineKeyboardButton("📈 보유 현황",   callback_data="CMD:holdings"),
            ],
            [
                InlineKeyboardButton("🏆 졸업 전당",   callback_data="CMD:history"),
                InlineKeyboardButton("🔄 DB 동기화",   callback_data="SYNCDB:VIEW"),
            ],
        ]
        await update.effective_message.reply_text(
            msg, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    # ----------------------------------------------------------
    # /sync — 통합 지시서 (핵심!)
    # ----------------------------------------------------------
    async def cmd_sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        msg_obj = await update.effective_message.reply_text(
            "🔄 시장 분석 및 지시서 작성 중...", parse_mode="HTML"
        )
        try:
            await self._send_sync_report(msg_obj, context)
        except Exception as e:
            log.exception("[TG] sync 실패")
            await msg_obj.edit_text(f"❌ 지시서 생성 실패: {e}")
    async def _send_sync_report(self, msg_obj, context):
        now      = datetime.datetime.now(KST)
        mode_str = "🔴 실전" if self.cfg.get("TRADE_MODE", "MOCK") == "REAL" else "🟡 모의"
        mkt      = self._market_status()
        # 잔고 조회
        try:
            balance      = await asyncio.to_thread(self.broker.get_balance)
            deposit      = balance.get("deposit", 0)
            withdrawable = balance.get("withdrawable", 0)
        except Exception:
            deposit = withdrawable = 0
        # 보유 종목
        try:
            holdings_list = await asyncio.to_thread(self.broker.get_holdings)
            holdings      = {h["code"]: h for h in holdings_list}
        except Exception:
            holdings = {}
        syms = self._get_active_symbols()
        lines = [
            f"📋 <b>[ 통합 지시서 ({mkt}) ]</b>",
            f"🕐 {now.strftime('%Y-%m-%d %H:%M')} KST  |  {mode_str}",
            f"💰 주문가능금액: {self._fmt_krw(withdrawable)}",
        ]
        inline_btns = []
        for s in syms:
            code      = s["code"]
            name      = s["name"]
            split     = s.get("split_count", 10)
            target_r  = s.get("target_profit_pct", 5.0) / 100.0
            allocation= s.get("allocation_krw", 0)
            one_port  = allocation // split if split > 0 else allocation
            mode_icon = "💎" if s.get("mode", "INFINITE") == "INFINITE" else "⚖️"
            mode_label= "무한매매" if s.get("mode", "INFINITE") == "INFINITE" else "V-REV"
            pos       = self.db.get_position(code) or {}
            avg_price = int(pos.get("avg_price", 0))
            total_qty = int(pos.get("total_qty", 0))
            round_no  = pos.get("round_no", 1)
            # 실시간 현재가 + 고가/저가 (ka10001)
            try:
                info       = await asyncio.to_thread(self.broker.get_stock_info, code)
                cur        = info.get("cur_price",  0)
                prev_close = info.get("prev_close", 0)
                day_high   = info.get("day_high",   0)
                day_low    = info.get("day_low",    0)
                change_pct = info.get("change_pct", 0.0)
            except Exception:
                cur = prev_close = day_high = day_low = 0
                change_pct = 0.0
            # ── 브로커 보유 잔고 + 핵심 계산 ─────────────────────
            broker_h  = holdings.get(code, {})
            b_qty     = int(broker_h.get("qty", 0))
            b_avg     = int(broker_h.get("avg_price", 0))
            qty = b_qty if b_qty > 0 else total_qty
            avg = b_avg if b_avg > 0 else avg_price

            sync_info    = self.engine.build_sync_plan(
                s, cur, {"avg_price": avg, "total_qty": qty} if qty > 0 else {}
            )
            t_val        = sync_info["t_val"]
            star_ratio   = sync_info["star_ratio"]
            star_price   = sync_info["star_price"]
            target_price = sync_info["target_price"]
            large_num    = sync_info["large_num"]
            plan_result  = sync_info["plan"]
            phase        = sync_info["phase"]

            # 수익률
            if qty > 0 and avg > 0 and cur > 0:
                profit_pct = (cur - avg) / avg * 100
                profit_amt = (cur - avg) * qty
            else:
                profit_pct = profit_amt = 0

            # 수익률
            if qty > 0 and avg > 0 and cur > 0:
                profit_pct  = (cur - avg) / avg * 100
                profit_amt  = (cur - avg) * qty
            else:
                profit_pct = profit_amt = 0

            # ── 종목 섹션 출력 ─────────────────────────────────
            lines += [
                "",
                f"{mode_icon} <b>[{name}] {mode_label} ({round_no}회차)</b>",
                f"📈 진행: <b>{t_val:.2f}T / {split}분할</b>  [{phase}]",
                f"💵 총 할당: {self._fmt_krw(allocation)}  |  회차예산: {self._fmt_krw(one_port)}",
            ]
            if cur > 0:
                chg_icon = "🔺" if change_pct >= 0 else "🔻"
                chg_sign = "+" if change_pct >= 0 else ""
                lines.append(
                    f"💱 현재 {self._fmt_krw(cur)} ({chg_icon}{chg_sign}{change_pct:.2f}%) "
                    f"/ 평단 {self._fmt_krw(avg)} ({qty:,}주)"
                )
            if day_high > 0:
                lines.append(
                    f"📈 금일 고가: {self._fmt_krw(day_high)} "
                    f"/ 저가: {self._fmt_krw(day_low)}"
                )
            if qty > 0 and avg > 0 and cur > 0:
                icon = "🔺" if profit_amt >= 0 else "🔻"
                lines.append(
                    f"{icon} 수익: <b>{self._fmt_pct(profit_pct)}</b> "
                    f"({self._fmt_krw(profit_amt)})"
                )

            lines.append("")
            if avg > 0:
                lines += [
                    f"⚙️ 익절목표: <b>{self._fmt_krw(target_price)}</b> (+{target_r*100:.1f}%)",
                    f"⭐ 별값: {self._fmt_pct(star_ratio*100)} | 별값가: {self._fmt_krw(star_price)}",
                    f"🔶 큰수: {self._fmt_krw(large_num)} ({s.get('large_num_pct',15.0):.0f}%)",
                ]
            elif cur > 0:
                # 새출발 예상값
                lines += [
                    f"⚙️ 진입시 예상 목표: <b>{self._fmt_krw(target_price)}</b> (+{target_r*100:.1f}%)",
                    f"⭐ 진입시 예상 별값: {self._fmt_krw(star_price)}",
                    f"🔶 큰수(진입 기준가): {self._fmt_krw(large_num)} ({s.get('large_num_pct',15.0):.0f}%)",
                ]

            # ── 주문 계획 표시 ──────────────────────────────────
            buy_orders  = plan_result.get("buy",  [])
            sell_orders = plan_result.get("sell", [])

            if buy_orders or sell_orders:
                lines.append(f"📋 <b>[ 주문 계획 — {phase} ]</b>")

                # ── 매수 섹션
                if buy_orders:
                    lines.append("  <b>▼ 매수</b>")
                    # 기본 매수 주문 (줍줍 제외)
                    main_buys = [o for o in buy_orders if "줍줍" not in o["desc"]]
                    jub_buys  = [o for o in buy_orders if "줍줍"     in o["desc"]]
                    for o in main_buys:
                        lines.append(
                            f"  🔴 [{o['type']}] {o['desc']}"
                            f"  {self._fmt_krw(o['price'])} × {o['qty']:,}주"
                        )
                    if jub_buys:
                        # 줍줍은 가격 범위로 요약
                        min_p = min(o["price"] for o in jub_buys)
                        max_p = max(o["price"] for o in jub_buys)
                        lines.append(
                            f"  🧹 [동시호가] 줍줍 {len(jub_buys)}건"
                            f"  ({self._fmt_krw(min_p)} ~ {self._fmt_krw(max_p)}) × 각 1주"
                        )
                        for o in jub_buys:
                            lines.append(
                                f"      └ {o['desc']}: {self._fmt_krw(o['price'])} 이하"
                            )

                # ── 구분선
                if buy_orders and sell_orders:
                    lines.append("")

                # ── 매도 섹션
                if sell_orders:
                    label = "▼ 매도 [진입후 예상]" if "진입후 예상" in (sell_orders[0].get("desc","")) else "▼ 매도"
                    lines.append(f"  <b>{label}</b>")
                    for o in sell_orders:
                        desc_clean = o["desc"].replace(" [진입후 예상]", "")
                        lines.append(
                            f"  🔵 [{o['type']}] {desc_clean}"
                            f"  {self._fmt_krw(o['price'])} × {o['qty']:,}주"
                        )

            elif cur <= 0:
                lines.append("  ⚠️ 현재가 조회 불가 — 15:10 재시도")
            lines.append("")
            inline_btns.append([
                InlineKeyboardButton(
                    f"📊 {name} 장부", callback_data=f"RECORD:{code}"
                ),
                InlineKeyboardButton(
                    f"⚙️ {name} 설정", callback_data=f"SETTLE:{code}"
                ),
            ])
        lines.append(f"\n{mkt} | {now.strftime('%H:%M:%S')} KST")
        await msg_obj.edit_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_btns)
        )
    # ----------------------------------------------------------
    # /record — 장부 조회
    # ----------------------------------------------------------
    async def cmd_record(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        syms = self._get_active_symbols()
        keyboard = []
        msg = "📊 <b>장부 조회</b>\n조회할 종목을 선택하세요:\n"
        for s in syms:
            keyboard.append([
                InlineKeyboardButton(
                    f"📋 {s['name']}",
                    callback_data=f"RECORD:{s['code']}"
                )
            ])
        keyboard.append([InlineKeyboardButton("📋 전체 조회", callback_data="RECORD:ALL")])
        await update.effective_message.reply_text(
            msg, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    async def _show_record(self, code: str, query=None, msg_obj=None):
        sym = self._get_symbol(code)
        if not sym:
            txt = f"❌ {code} 종목을 찾을 수 없습니다."
            if query:
                await query.edit_message_text(txt)
            return
        name    = sym["name"]
        today   = datetime.date.today().isoformat()
        # 최근 10일 체결 내역
        all_trades = []
        for i in range(10):
            d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
            trades = self.db.get_trades_by_date(d)
            all_trades += [t for t in trades if t.get("code") == code]
        # 포지션
        pos       = self.db.get_position(code) or {}
        avg_price = int(pos.get("avg_price", 0))
        total_qty = int(pos.get("total_qty", 0))
        split     = sym.get("split_count", 10)
        allocation= sym.get("allocation_krw", 0)
        one_port  = allocation // split if split > 0 else allocation
        one_port_qty = calc_one_portion_qty(one_port, avg_price) if avg_price > 0 else 1
        t_val        = calc_t_val(total_qty, one_port_qty)
        lines = [
            f"📋 <b>[ {name}({code}) 장부 (최근 10일) ]</b>",
            "",
            f"{'No.':<4} {'일자':<6} {'구분':<4} {'평균단가':>10} {'수량':>5}",
            "─" * 10,
        ]
        for i, t in enumerate(all_trades[:15], 1):
            side_icon = "🔴매수" if t.get("side") == "BUY" else "🔵매도"
            date_str  = t.get("trade_date", "")[-5:].replace("-", ".")
            price_str = f"{int(t.get('price',0)):,}원"
            qty_str   = f"{int(t.get('qty',0)):,}주"
            lines.append(f"{i:<4} {date_str:<6} {side_icon} {price_str:>10} {qty_str:>6}")
        if not all_trades:
            lines.append("  (거래 내역 없음)")
        total_buy  = sum(t.get("amount", 0) for t in all_trades if t.get("side") == "BUY")
        total_sell = sum(t.get("amount", 0) for t in all_trades if t.get("side") == "SELL")
        lines += [
            "─" * 10,
            f"📊 <b>[ 현재 진행 상황 ]</b>",
            f"■ 현재 T값: {t_val:.4f}T ({split}분할)",
            f"■ 보유 수량: {total_qty:,}주 (평단 {self._fmt_krw(avg_price)})",
            f"■ 총 매수액: {self._fmt_krw(int(total_buy))}",
            f"■ 총 매도액: {self._fmt_krw(int(total_sell))}",
        ]
        keyboard = [[
            InlineKeyboardButton(
                f"🔄 {name} 장부 업데이트", callback_data=f"RECORD:UPDATE:{code}"
            )
        ]]
        txt = "\n".join(lines)
        if query:
            await query.edit_message_text(
                txt, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif msg_obj:
            await msg_obj.edit_text(
                txt, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    async def _show_record_to_chat(self, code: str, context, chat_id: int):
        """새 메시지로 장부 전송 (전체 조회용)."""
        sym = self._get_symbol(code)
        if not sym:
            await context.bot.send_message(chat_id, f"❌ {code} 종목 없음")
            return
        name = sym["name"]
        today = datetime.date.today().isoformat()
        all_trades = []
        for i in range(10):
            d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
            all_trades += [t for t in self.db.get_trades_by_date(d)
                           if t.get("code") == code]
        pos       = self.db.get_position(code) or {}
        avg_price = int(pos.get("avg_price", 0))
        total_qty = int(pos.get("total_qty", 0))
        split     = sym.get("split_count", 10)
        allocation= sym.get("allocation_krw", 0)
        one_port  = allocation // split if split > 0 else allocation
        one_port_qty = calc_one_portion_qty(one_port, avg_price) if avg_price > 0 else 1
        t_val    = calc_t_val(total_qty, one_port_qty)
        lines    = [f"📋 <b>[ {name}({code}) 장부 ]</b>", ""]
        for i, t in enumerate(all_trades[:10], 1):
            side_icon = "🔴매수" if t.get("side") == "BUY" else "🔵매도"
            date_str  = t.get("trade_date","")[-5:].replace("-",".")
            price_str = f"{int(t.get('price',0)):,}원"
            qty_str   = f"{int(t.get('qty',0)):,}주"
            lines.append(f"{i} {date_str} {side_icon} {price_str} {qty_str}")
        total_buy  = sum(t.get("amount",0) for t in all_trades if t.get("side")=="BUY")
        total_sell = sum(t.get("amount",0) for t in all_trades if t.get("side")=="SELL")
        lines += [
            "─" * 10,
            f"T값: {t_val:.4f}T | 보유: {total_qty:,}주 (평단 {self._fmt_krw(avg_price)})",
            f"총매수: {self._fmt_krw(int(total_buy))} | 총매도: {self._fmt_krw(int(total_sell))}",
        ]
        kb = [[InlineKeyboardButton(f"🔄 {name} 업데이트",
                                    callback_data=f"RECORD:UPDATE:{code}")]]
        await context.bot.send_message(
            chat_id, "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    # ----------------------------------------------------------
    # /settlement — 설정 현황 및 파라미터 변경
    # ----------------------------------------------------------
    async def cmd_settlement(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        await self._show_settlement(update.message)
    async def _show_settlement(self, msg, query=None):
        syms   = self._get_symbols()
        lines  = ["⚙️ <b>[ 현재 설정 및 운영 상태 ]</b>", ""]
        boards = []
        for s in syms:
            code   = s["code"]
            name   = s["name"]
            mode   = s.get("mode", "INFINITE")
            active = s.get("active", True)
            split  = s.get("split_count", 10)
            target = s.get("target_profit_pct", 5.0)
            alloc  = s.get("allocation_krw", 0)
            band   = s.get("vrev_band_pct", 3.0)
            limit  = s.get("daily_buy_limit_krw", 0)
            mode_label = "무한매매 (LOC)" if mode == "INFINITE" else "V-REV 리밸런싱"
            act_icon   = "✅" if active else "🔇"
            avwap_budget = s.get("avwap_budget", 0)
            avwap_icon   = "⚡️ ON" if avwap_budget > 0 else "💤 OFF"
            lines += [
                f"{act_icon} <b>{name} ({code})</b>  [{mode_label}]",
                f"  분할: {split}회 | 목표: {target:.1f}% | 밴드: {band:.1f}%",
                f"  할당금: {self._fmt_krw(alloc)} | 일한도: {self._fmt_krw(limit)}",
                f"  AVWAP: {avwap_icon}"
                + (f" | 예산: {self._fmt_krw(avwap_budget)}" if avwap_budget > 0 else ""),
                "",
            ]
            # 종목별 헤더 + 버튼 행 (색상으로 구분)
            _icons = ["🔵","🟢","🟠","🔴","🟣","🟡"]
            _all_codes = [s["code"] for s in self._get_symbols()]
            _idx = _all_codes.index(code) if code in _all_codes else 0
            t_icon = _icons[_idx % len(_icons)]
            act_txt  = "✅ 활성" if active else "🔇 비활성"
            mode_txt = "무한→VREV" if mode=="INFINITE" else "VREV→무한"
            alloc_m  = alloc // 10000
            limit_m  = limit // 10000
            # ── 헤더: 전체 너비 1열, 종목 구분용 ──────────
            boards.append([
                InlineKeyboardButton(
                    f"{t_icon} {name} ({code}) {t_icon}",
                    callback_data=f"RECORD:{code}"
                ),
            ])
            # ── 활성/모드 ────────────
            boards.append([
                InlineKeyboardButton(f"{act_txt}",  callback_data=f"SETTLE:TOGGLE:{code}"),
                InlineKeyboardButton(f"{mode_txt}", callback_data=f"SETTLE:MODE:{code}"),
            ])
            # ── 세부 설정 ────────────
            boards.append([
                InlineKeyboardButton(f"분할 ({split}회)",    callback_data=f"SETTLE:SET:split:{code}"),
                InlineKeyboardButton(f"목표 ({target:.0f}%)", callback_data=f"SETTLE:SET:target:{code}"),
                InlineKeyboardButton(f"밴드 ({band:.0f}%)",  callback_data=f"SETTLE:SET:band:{code}"),
            ])
            boards.append([
                InlineKeyboardButton(f"💰 할당금 ({alloc_m}만원)", callback_data=f"SETTLE:SET:alloc:{code}"),
                InlineKeyboardButton(f"📅 일한도 ({limit_m}만원)", callback_data=f"SETTLE:SET:limit:{code}"),
            ])
            # ── AVWAP 설정 ────────────
            avwap_budget = s.get("avwap_budget", 0)
            avwap_toggle_txt = "⚡️ AVWAP ON" if avwap_budget > 0 else "💤 AVWAP OFF"
            avwap_budget_m   = avwap_budget // 10000
            boards.append([
                InlineKeyboardButton(
                    avwap_toggle_txt,
                    callback_data=f"SETTLE:AVWAP:TOGGLE:{code}"
                ),
                InlineKeyboardButton(
                    f"⚡️예산 ({avwap_budget_m}만원)" if avwap_budget > 0 else "⚡️예산 설정",
                    callback_data=f"SETTLE:AVWAP:BUDGET:{code}"
                ),
            ])
        txt = "\n".join(lines)
        if query:
            await query.edit_message_text(
                txt, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(boards)
            )
        elif msg:
            await msg.reply_text(
                txt, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(boards)
            )
    # ----------------------------------------------------------
    # /ticker — 종목 관리
    # ----------------------------------------------------------
    async def cmd_ticker(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        await self._show_ticker_menu(update.message)
    async def _show_ticker_menu(self, msg, query=None):
        syms = self._get_symbols()
        lines = ["🔄 <b>[ 종목 관리 ]</b>", ""]
        keyboard = []
        for s in syms:
            code   = s["code"]
            name   = s["name"]
            active = s.get("active", True)
            mode   = s.get("mode", "INFINITE")
            icon   = "✅" if active else "🔇"
            m_icon = "💎" if mode == "INFINITE" else "⚖️"
            lines.append(f"{icon} {m_icon} {name} ({code})")
            keyboard.append([
                InlineKeyboardButton(
                    f"{'✅ 활성화됨' if active else '🔇 비활성화됨'}  {name}",
                    callback_data=f"TICKER:TOGGLE:{code}"
                ),
                InlineKeyboardButton(
                    "🗑 제거", callback_data=f"TICKER:REMOVE:{code}"
                ),
            ])
        keyboard.append([
            InlineKeyboardButton("➕ 새 종목 추가", callback_data="TICKER:ADD")
        ])
        keyboard.append([
            InlineKeyboardButton("🔄 기본 종목 복원", callback_data="TICKER:RESET")
        ])
        txt = "\n".join(lines)
        if query:
            await query.edit_message_text(
                txt, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif msg:
            await msg.reply_text(
                txt, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    # ----------------------------------------------------------
    # /mode — INFINITE / V-REV 전환
    # ----------------------------------------------------------
    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        syms     = self._get_active_symbols()
        keyboard = []
        lines    = ["🎯 <b>[ 모드 전환 ]</b>", ""]
        for s in syms:
            code = s["code"]
            name = s["name"]
            mode = s.get("mode", "INFINITE")
            icon = "💎" if mode == "INFINITE" else "⚖️"
            lines.append(f"{icon} {name}: {'무한매매' if mode=='INFINITE' else 'V-REV'}")
            keyboard.append([
                InlineKeyboardButton(
                    f"{name} → {'V-REV' if mode=='INFINITE' else '무한매매'}",
                    callback_data=f"SETTLE:MODE:{code}"
                )
            ])
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    # ----------------------------------------------------------
    # /seed — 시드머니 설정
    # ----------------------------------------------------------
    async def cmd_seed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        syms     = self._get_active_symbols()
        keyboard = []
        lines    = ["💵 <b>[ 시드머니(할당금) 관리 ]</b>", ""]
        for s in syms:
            code  = s["code"]
            name  = s["name"]
            alloc = s.get("allocation_krw", 0)
            lines.append(f"◆ {name}: {self._fmt_krw(alloc)}")
            keyboard.append([
                InlineKeyboardButton(f"💵 {name} 변경", callback_data=f"SETTLE:SET:alloc:{code}")
            ])
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    # ----------------------------------------------------------
    # /balance /holdings /report /pause /resume /cancel /help
    # ----------------------------------------------------------
    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        m = await update.effective_message.reply_text("💰 잔고 조회 중...")
        try:
            b = await asyncio.to_thread(self.broker.get_balance)
            await m.edit_text(
                f"💰 <b>계좌 잔고</b>\n"
                f"📥 예수금:    {self._fmt_krw(b.get('deposit',0))}\n"
                f"💳 출금가능:  {self._fmt_krw(b.get('withdrawable',0))}\n"
                f"📊 평가금액:  {self._fmt_krw(b.get('eval_total',0))}\n"
                f"💹 평가손익:  {self._fmt_krw(b.get('eval_profit',0))} "
                f"({self._fmt_pct(b.get('profit_pct',0))})\n"
                f"🕐 {datetime.datetime.now(KST).strftime('%H:%M:%S')} KST",
                parse_mode="HTML"
            )
        except Exception as e:
            await m.edit_text(f"❌ 잔고 조회 실패: {e}")
    async def cmd_holdings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        m = await update.effective_message.reply_text("📈 보유 종목 조회 중...")
        try:
            hs = await asyncio.to_thread(self.broker.get_holdings)
            if not hs:
                await m.edit_text("📭 현재 보유 종목이 없습니다.", parse_mode="HTML")
                return
            lines = ["📈 <b>보유 종목 평가손익</b>", ""]
            total_profit = 0
            for h in hs:
                profit = h.get("profit", 0)
                pct    = h.get("profit_pct", 0.0)
                icon   = "🟢" if profit >= 0 else "🔴"
                sign   = "+" if profit >= 0 else ""
                lines.append(
                    f"{icon} <b>{h.get('name','')}({h.get('code','')})</b>\n"
                    f"   {h.get('qty',0):,}주 | 평단 {self._fmt_krw(h.get('avg_price',0))} "
                    f"| 현재 {self._fmt_krw(h.get('current_price',0))}\n"
                    f"   손익: <b>{sign}{self._fmt_krw(profit)}</b> ({sign}{pct:.2f}%)"
                )
                total_profit += profit
            sign_t = "+" if total_profit >= 0 else ""
            lines += [
                f"💼 총 평가손익: <b>{sign_t}{self._fmt_krw(total_profit)}</b>",
            ]
            await m.edit_text("\n".join(lines), parse_mode="HTML")
        except Exception as e:
            await m.edit_text(f"❌ 보유 종목 조회 실패: {e}")
    async def cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        today  = datetime.date.today().isoformat()
        trades = self.db.get_trades_by_date(today)
        lines  = [f"📋 <b>당일 정산 ({today})</b>", ""]
        syms   = self._get_active_symbols()
        for s in syms:
            code = s["code"]
            name = s["name"]
            st   = [t for t in trades if t.get("code") == code]
            buys = [t for t in st if t.get("side") == "BUY"]
            sells= [t for t in st if t.get("side") == "SELL"]
            buy_amt  = sum(t.get("amount", 0) for t in buys)
            sell_amt = sum(t.get("amount", 0) for t in sells)
            pnl      = sell_amt - buy_amt
            sign     = "+" if pnl >= 0 else ""
            if st:
                lines += [
                    f"◆ <b>{name}</b>",
                    f"  매수 {len(buys)}건: {self._fmt_krw(int(buy_amt))}",
                    f"  매도 {len(sells)}건: {self._fmt_krw(int(sell_amt))}",
                    f"  당일 손익: <b>{sign}{self._fmt_krw(int(pnl))}</b>",
                ]
        if not any(
            self.db.get_trades_by_date(today)
            for _ in [1]
        ):
            lines.append("  오늘 체결 내역 없음")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")
    # ----------------------------------------------------------
    # /buy — 수동 매수 주문
    # 사용법: /buy [코드] [수량] [가격(0=시장가)]
    # 예시:   /buy 122630 5 15000   (지정가)
    #         /buy 122630 5          (시장가)
    # ----------------------------------------------------------
    # ==========================================================
    # 수동 매수/매도 UI (4단계 인라인 버튼 흐름)
    # Step1 종목선택 -> Step2 수량선택 -> Step3 가격선택 -> Step4 확인
    # ==========================================================

    async def cmd_buy(self, update, context):
        if not self._is_admin(update): return
        await self._show_trade_ticker(update.effective_message, "BUY")

    async def cmd_sell(self, update, context):
        if not self._is_admin(update): return
        await self._show_trade_ticker(update.effective_message, "SELL")

    async def _show_trade_ticker(self, msg, side, query=None):
        syms  = self._get_active_symbols()
        icon  = "🔴" if side == "BUY" else "🔵"
        label = "매수" if side == "BUY" else "매도"
        pre   = "BUY" if side == "BUY" else "SEL"
        kb = []
        for s in syms:
            code = s["code"]
            pos  = self.db.get_position(code) or {}
            qty  = int(pos.get("total_qty", 0))
            hold = f" ({qty:,}주 보유)" if qty > 0 else ""
            kb.append([InlineKeyboardButton(
                f"{icon} {s['name']} ({code}){hold}",
                callback_data=f"{pre}:T:{code}")])
        kb.append([InlineKeyboardButton("❌ 취소", callback_data="TRADE:CANCEL")])
        txt = f"{icon} <b>수동 {label} — 종목 선택</b>"
        if query:
            await query.edit_message_text(txt, parse_mode="HTML",
                                          reply_markup=InlineKeyboardMarkup(kb))
        else:
            await msg.reply_text(txt, parse_mode="HTML",
                                 reply_markup=InlineKeyboardMarkup(kb))

    async def _show_trade_qty(self, query, side, code):
        sym   = self._get_symbol(code)
        name  = sym["name"] if sym else code
        icon  = "🔴" if side == "BUY" else "🔵"
        label = "매수" if side == "BUY" else "매도"
        pre   = "BUY" if side == "BUY" else "SEL"
        try:
            cur = await asyncio.to_thread(self.broker.get_current_price, code)
        except Exception:
            cur = 0
        pos      = self.db.get_position(code) or {}
        hold_qty = int(pos.get("total_qty", 0))
        avg_px   = int(pos.get("avg_price", 0))
        info = [f"{icon} <b>수동 {label} — 수량 선택</b>",
                f"종목: <b>{name}</b>  현재가: <b>{self._fmt_krw(cur)}</b>"]
        if side == "SELL" and hold_qty > 0:
            info.append(f"보유: {hold_qty:,}주  평단: {self._fmt_krw(avg_px)}")
        if side == "SELL" and hold_qty > 0:
            qty_list = sorted(set([
                max(1, hold_qty//4), max(1, hold_qty//2), hold_qty,
                1, 3, 5, 10]))
        else:
            qty_list = [1, 3, 5, 10, 20, 50, 100]
        kb, row = [], []
        for q in qty_list:
            lbl = (f"전량 {q}주" if side == "SELL" and q == hold_qty
                   else f"{q}주")
            row.append(InlineKeyboardButton(
                lbl, callback_data=f"{pre}:Q:{code}:{q}"))
            if len(row) == 3:
                kb.append(row); row = []
        if row: kb.append(row)
        kb.append([InlineKeyboardButton(
            "✏️ 직접 입력", callback_data=f"{pre}:Q:{code}:M")])
        kb.append([InlineKeyboardButton("◀ 종목 재선택", callback_data=f"{pre}:BACK"),
                   InlineKeyboardButton("❌ 취소", callback_data="TRADE:CANCEL")])
        await query.edit_message_text(
            "\n".join(info), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb))

    async def _show_trade_price(self, query, side, code, qty):
        sym   = self._get_symbol(code)
        name  = sym["name"] if sym else code
        icon  = "🔴" if side == "BUY" else "🔵"
        label = "매수" if side == "BUY" else "매도"
        pre   = "BUY" if side == "BUY" else "SEL"
        try:
            cur = await asyncio.to_thread(self.broker.get_current_price, code)
        except Exception:
            cur = 0
        from kiwoom_api import round_to_tick as rtt
        info = [f"{icon} <b>수동 {label} — 가격 선택</b>",
                f"종목: <b>{name}</b>  수량: <b>{qty:,}주</b>",
                f"현재가: <b>{self._fmt_krw(cur)}</b>"]
        prices = [("시장가", 0)]
        if cur > 0:
            prices += [
                (f"현재가 {self._fmt_krw(cur)}", cur),
                (f"+0.5% {self._fmt_krw(rtt(int(cur*1.005)))}", rtt(int(cur*1.005))),
                (f"+1%   {self._fmt_krw(rtt(int(cur*1.01)))}", rtt(int(cur*1.01))),
                (f"-0.5% {self._fmt_krw(rtt(int(cur*0.995)))}", rtt(int(cur*0.995))),
                (f"-1%   {self._fmt_krw(rtt(int(cur*0.99)))}", rtt(int(cur*0.99))),
            ]
        kb, row = [], []
        for plabel, pval in prices:
            row.append(InlineKeyboardButton(
                plabel, callback_data=f"{pre}:P:{code}:{qty}:{pval}"))
            if len(row) == 2:
                kb.append(row); row = []
        if row: kb.append(row)
        kb.append([InlineKeyboardButton(
            "✏️ 직접 입력", callback_data=f"{pre}:P:{code}:{qty}:M")])
        kb.append([InlineKeyboardButton(f"◀ 수량 재선택", callback_data=f"{pre}:T:{code}"),
                   InlineKeyboardButton("❌ 취소", callback_data="TRADE:CANCEL")])
        await query.edit_message_text(
            "\n".join(info), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb))

    async def _show_trade_confirm(self, query, side, code, qty, price):
        sym   = self._get_symbol(code)
        name  = sym["name"] if sym else code
        icon  = "🔴" if side == "BUY" else "🔵"
        label = "매수" if side == "BUY" else "매도"
        type_str = "시장가" if price == 0 else f"{price:,}원 지정가"
        amount   = qty * price if price > 0 else 0
        lines = [f"{icon} <b>수동 {label} — 최종 확인</b>",
                 f"종목: <b>{name}</b> ({code})",
                 f"수량: <b>{qty:,}주</b>  가격: <b>{type_str}</b>"]
        if amount > 0:
            lines.append(f"예상금액: <b>{self._fmt_krw(amount)}</b>")
        lines.append("")
        lines.append("주문을 실행하시겠습니까?")
        kb = [[InlineKeyboardButton(f"✅ {label} 실행",
                callback_data=f"MANORDER:{side}:{code}:{qty}:{price}"),
               InlineKeyboardButton("❌ 취소", callback_data="TRADE:CANCEL")]]
        await query.edit_message_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb))

    async def _execute_manual_buy(self, msg, code, qty, price):
        sym  = self._get_symbol(code)
        name = sym["name"] if sym else code
        type_str = "시장가" if price == 0 else f"{price:,}원 지정가"
        kb = [[InlineKeyboardButton("✅ 매수 실행",
                callback_data=f"MANORDER:BUY:{code}:{qty}:{price}"),
               InlineKeyboardButton("❌ 취소", callback_data="TRADE:CANCEL")]]
        await msg.reply_text(
            f"🔴 <b>매수 확인</b>  {name} ({code})\n"
            f"수량: {qty:,}주  가격: {type_str}",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    async def _execute_manual_sell(self, msg, code, qty, price):
        sym  = self._get_symbol(code)
        name = sym["name"] if sym else code
        type_str = "시장가" if price == 0 else f"{price:,}원 지정가"
        kb = [[InlineKeyboardButton("✅ 매도 실행",
                callback_data=f"MANORDER:SELL:{code}:{qty}:{price}"),
               InlineKeyboardButton("❌ 취소", callback_data="TRADE:CANCEL")]]
        await msg.reply_text(
            f"🔵 <b>매도 확인</b>  {name} ({code})\n"
            f"수량: {qty:,}주  가격: {type_str}",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    # ==========================================================
    # /sync_db — 키움 잔고 → DB 동기화 UI
    # ==========================================================

    async def cmd_sync_db(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        await self._show_syncdb_menu(update.effective_message)

    async def _show_syncdb_menu(self, msg, query=None):
        """동기화 전 현황 비교 화면."""
        db_positions = self.db.get_all_positions()
        db_map = {p['code']: p for p in db_positions if int(p.get('total_qty',0)) > 0}
        try:
            holdings = await asyncio.to_thread(self.broker.get_holdings)
            kiwoom_map = {h['code']: h for h in holdings}
            api_ok = True
        except Exception:
            kiwoom_map = {}
            api_ok = False
        now = datetime.datetime.now(KST).strftime('%H:%M:%S')
        lines = [
            '🔄 <b>키움 → DB 동기화</b>',
            f'🕐 {now} KST  |  {"✅ API 연결" if api_ok else "❌ API 오류"}',
            '',
            '<b>현재 상태 비교:</b>',
        ]
        if kiwoom_map:
            for code, h in kiwoom_map.items():
                name  = h.get('name', code)
                k_qty = int(h.get('qty', 0))
                k_avg = int(h.get('avg_price', 0))
                d     = db_map.get(code, {})
                d_qty = int(d.get('total_qty', 0))
                d_avg = int(d.get('avg_price', 0))
                ok = (k_qty == d_qty and k_avg == d_avg)
                icon = '✅' if ok else '⚠️ 불일치'
                lines.append(
                    f'{icon} <b>{name}</b> ({code})  '
                    f'키움: {k_qty:,}주/{self._fmt_krw(k_avg)}  '
                    f'DB: {d_qty:,}주/{self._fmt_krw(d_avg)}'
                )
        else:
            lines.append('  📭 키움 보유 종목 없음')
        for code, d in db_map.items():
            if code not in kiwoom_map:
                d_qty = int(d.get('total_qty', 0))
                dn    = d.get('name', code)
                lines.append(
                    f'⚠️ <b>{dn}</b> ({code})  '
                    f'키움: 0주 / DB: {d_qty:,}주 (키움 미보유)'
                )
        lines += ['', '🔄 실행하면 <b>키움 잔고 기준으로 DB를 덮어씁니다.</b>']
        kb = [
            [InlineKeyboardButton('✅ 동기화 실행', callback_data='SYNCDB:EXEC')],
            [
                InlineKeyboardButton('🔍 새로고침', callback_data='SYNCDB:VIEW'),
                InlineKeyboardButton('❌ 닫기',    callback_data='SYNCDB:CLOSE'),
            ],
        ]
        txt_out = '\n'.join(lines)
        if query:
            await query.edit_message_text(txt_out, parse_mode='HTML',
                                          reply_markup=InlineKeyboardMarkup(kb))
        else:
            await msg.reply_text(txt_out, parse_mode='HTML',
                                 reply_markup=InlineKeyboardMarkup(kb))

    async def _do_sync_db(self, msg):
        """키움 잔고 기준 DB 강제 반영."""
        try:
            holdings = await asyncio.to_thread(self.broker.get_holdings)
            sym_map  = {s['code']: s for s in self._get_symbols()}
            updated  = []
            result_lines = ['✅ <b>동기화 완료</b>',
                            f'🕐 {datetime.datetime.now(KST).strftime("%H:%M:%S")} KST', '']
            for h in holdings:
                code  = h.get('code', '')
                name  = h.get('name', code)
                qty   = int(h.get('qty', 0))
                avg   = int(h.get('avg_price', 0))
                pct   = h.get('profit_pct', 0.0)
                old_pos = self.db.get_position(code) or {}
                old_qty = int(old_pos.get('total_qty', 0))
                old_avg = int(old_pos.get('avg_price', 0))
                sym_name = sym_map.get(code, {}).get('name', name)
                self.db.upsert_position(
                    code=code, name=sym_name or name,
                    avg_price=avg, total_qty=qty,
                    round_no=old_pos.get('round_no', 1),
                )
                updated.append(code)
                changed = (qty != old_qty or avg != old_avg)
                ic   = '🟢' if pct >= 0 else '🔴'
                sign = '+' if pct >= 0 else ''
                diff = f'  변경: {old_qty}주→{qty}주' if changed else '  (변경없음)'
                result_lines.append(
                    f'{ic} <b>{sym_name or name}</b> ({code})  '
                    f'{qty:,}주 / {self._fmt_krw(avg)}  '
                    f'{sign}{pct:.2f}%{diff}'
                )
            for pos in self.db.get_all_positions():
                c = pos.get('code', '')
                if c not in updated and int(pos.get('total_qty', 0)) > 0:
                    self.db.upsert_position(
                        code=c, name=pos.get('name', c),
                        avg_price=0, total_qty=0,
                        round_no=pos.get('round_no', 1) + 1,
                    )
                    result_lines.append(f'⚪ {c}: 잔량 0으로 초기화')
            if not holdings:
                result_lines.append('📭 보유 종목 없음')
            kb = [
                [InlineKeyboardButton('🔄 다시 동기화', callback_data='SYNCDB:EXEC')],
                [InlineKeyboardButton('🔍 현황 보기',   callback_data='SYNCDB:VIEW')],
            ]
            await msg.edit_text('\n'.join(result_lines), parse_mode='HTML',
                                reply_markup=InlineKeyboardMarkup(kb))
        except Exception as ex:
            log.exception('[TG] sync_db 실패')
            await msg.edit_text(f'❌ 동기화 실패: {ex}')

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        self.cfg.set("BOT_PAUSED", True)
        await update.effective_message.reply_text(
            "⏸ <b>매매 일시 중지</b>\n/resume 으로 재개", parse_mode="HTML"
        )
    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        self.cfg.set("BOT_PAUSED", False)
        await update.effective_message.reply_text("▶️ <b>매매 재개</b>", parse_mode="HTML")
    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        keyboard = [[
            InlineKeyboardButton("✅ 전량 취소 확인", callback_data="CANCEL:CONFIRM"),
            InlineKeyboardButton("❌ 중단",           callback_data="CANCEL:ABORT"),
        ]]
        await update.effective_message.reply_text(
            "⚠️ <b>미체결 주문 전량 취소</b>\n계속하시겠습니까?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    # ----------------------------------------------------------
    # /avwap — AVWAP 퀀트 엔진 현황 및 설정
    # ----------------------------------------------------------
    async def cmd_avwap(self, update, context):
        if not self._is_admin(update):
            return
        if not hasattr(self, "avwap_engine") or self.avwap_engine is None:
            await update.effective_message.reply_text(
                "⚡️ <b>AVWAP 엔진</b>\n"
                "현재 비활성 상태입니다.\n"
                "config.json의 SYMBOLS 중 mode=\'AVWAP\'를 설정하거나\n"
                "avwap_budget 값을 추가하세요.",
                parse_mode="HTML"
            )
            return

        txt = self.avwap_engine.get_status_text()
        syms = [s for s in self._get_symbols() if s.get("avwap_budget", 0) > 0]
        kb = []
        for s in syms:
            code = s["code"]
            kb.append([
                InlineKeyboardButton(
                    f"⚡️ {s['name']} 상세",
                    callback_data=f"AVWAP:STATUS:{code}"
                )
            ])
        kb.append([
            InlineKeyboardButton("🔄 새로고침", callback_data="AVWAP:REFRESH"),
        ])
        await update.effective_message.reply_text(
            txt, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    def set_avwap_engine(self, engine):
        """main.py에서 AVWAPEngine 인스턴스 주입."""
        self.avwap_engine = engine

    # ----------------------------------------------------------
    # /history — 졸업 명예의 전당
    # ----------------------------------------------------------
    async def cmd_history(self, update, context):
        if not self._is_admin(update):
            return

        syms     = self._get_symbols()
        sym_map  = {s["code"]: s for s in syms}
        all_logs = self.db.get_cycle_log(limit=50)
        stats    = self.db.get_cycle_stats()

        if not all_logs:
            await update.effective_message.reply_text(
                "🏆 <b>졸업 명예의 전당</b>\n\n"
                "아직 완료된 사이클이 없습니다.\n"
                "무한매매 목표가 도달 시 자동으로 기록됩니다.",
                parse_mode="HTML"
            )
            return

        # 전체 통계
        sign_t = "+" if stats.get("total_profit", 0) >= 0 else ""
        lines  = [
            "🏆 <b>[ 졸업 명예의 전당 ]</b>",
            "",
            f"📊 <b>전체 통계</b>",
            f"  총 졸업: {stats.get('total', 0)}회  "
            f"승률: {stats.get('win_rate', 0):.1f}%",
            f"  총 수익: <b>{sign_t}{self._fmt_krw(stats.get('total_profit', 0))}</b>",
            f"  평균 수익률: {stats.get('avg_pct', 0):+.2f}%",
            f"  최고: {stats.get('best_pct', 0):+.2f}%  "
            f"최저: {stats.get('worst_pct', 0):+.2f}%",
            "",
        ]

        # 종목별 통계
        codes_seen = list(dict.fromkeys(l["code"] for l in all_logs))
        if len(codes_seen) > 1:
            lines.append("<b>종목별 요약</b>")
            for code in codes_seen:
                s = self.db.get_cycle_stats(code)
                name = sym_map.get(code, {}).get("name", code)
                if s:
                    sign = "+" if s["total_profit"] >= 0 else ""
                    lines.append(
                        f"  {name}: {s['total']}회 / "
                        f"{sign}{self._fmt_krw(s['total_profit'])} "
                        f"({s['win_rate']:.0f}%)"
                    )
            lines.append("")

        # 최근 졸업 기록
        lines.append("<b>최근 졸업 기록</b>")
        lines.append(
            f"{'No.':<3} {'종목':<10} {'회차':>3} "
            f"{'수익률':>7} {'수익금':>12} {'날짜'}"
        )
        lines.append("─" * 10)

        for i, log in enumerate(all_logs[:20], 1):
            name    = sym_map.get(log["code"], {}).get("name", log["code"])
            # 종목명 8자 이하로 단축
            short   = name[:8] if len(name) > 8 else name
            pct     = log["profit_pct"]
            profit  = log["profit"]
            sign    = "+" if profit >= 0 else ""
            icon    = "🏅" if pct >= 5 else ("✅" if pct >= 0 else "⚠️")
            date_s  = log["end_date"][5:] if log["end_date"] else "--"

            lines.append(
                f"{icon} <b>{short}</b>  "
                f"{log['round_no']}회차  "
                f"{sign}{pct:.2f}%  "
                f"{sign}{self._fmt_krw(profit)}  "
                f"{date_s}"
            )

        # 종목별 상세 버튼
        kb = []
        for code in codes_seen[:3]:
            name = sym_map.get(code, {}).get("name", code)
            kb.append([InlineKeyboardButton(
                f"🏆 {name} 상세",
                callback_data=f"HISTORY:{code}"
            )])

        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb) if kb else None
        )

    # ----------------------------------------------------------
    # /version — 버전 정보 및 업데이트 내역
    # ----------------------------------------------------------
    async def cmd_version(self, update, context):
        if not self._is_admin(update):
            return

        mode_str  = "🔴 실전" if self.cfg.get("TRADE_MODE","MOCK") == "REAL" else "🟡 모의투자"

        # 활성 종목 요약
        syms      = self._get_active_symbols()
        sym_lines = ""
        for s in syms:
            mode  = s.get("mode", "INFINITE")
            ab    = s.get("avwap_budget", 0)
            icons = "💎" if mode == "INFINITE" else "⚖️"
            avwap = "  ⚡️AVWAP ON" if ab > 0 else ""
            sym_lines += f"  {icons} {s['name']} ({s['code']}){avwap}\n"

        syms_for_ver = self._get_active_symbols()
        msg = get_telegram_version_text(syms_for_ver) or (
            f"🔧 <b>[ 버전 및 업데이트 내역 ]</b>\n"
            f"\n"
            f"🚀 <b>국내 ETF 무한매매 + AVWAP 봇</b>\n"
            f"📌 현재 버전: <b>v5.0</b>\n"
            f"💹 운영 모드: {mode_str}\n"
            f"\n"
            f"<b>[ 운용 종목 ]</b>\n"
            f"{sym_lines}"
            f"\n"
            f"<b>[ 업데이트 히스토리 ]</b>\n"
            f"\n"
            f"⚡️ <b>v5.0</b>  2026-05-24\n"
            f"  · AVWAP 퀀트 엔진 탑재 (승승장군 V44~V79)\n"
            f"  · ATR5 잔여체력 기반 다이나믹 목표가\n"
            f"  · 타임쉴드 09:30 / 강제청산 15:20\n"
            f"  · 무한매매·AVWAP 독립 예산 운용\n"
            f"  · 상세 매매 알림 (진입·청산·하드스탑)\n"
            f"  · /avwap 실시간 엔진 현황\n"
            f"  · settlement UI에 AVWAP 토글·예산 설정\n"
            f"  · AVWAP 백테스트 2년 +301% (복리)\n"
            f"\n"
            f"💎 <b>v4.0</b>  2026-05-24\n"
            f"  · 구글 시트 알고리즘 동기화 (큰수·줍줍)\n"
            f"  · plan_new_entry 줍줍 5개 완성\n"
            f"  · /sync 통합 지시서 (매수·매도 구분 UI)\n"
            f"  · 수동 매수·매도 4단계 인라인 UI\n"
            f"  · /sync_db 키움→DB 동기화 UI\n"
            f"  · 호가단위 전 가격 자동 적용\n"
            f"\n"
            f"⚖️ <b>v3.2</b>  2026-05-24\n"
            f"  · 텔레봇 통합 지시서·장부·설정·종목관리\n"
            f"  · /ticker 종목 추가·제거·활성화 UI\n"
            f"  · update.effective_message 일괄 적용\n"
            f"\n"
            f"🔄 <b>v3.0</b>  2026-05-20\n"
            f"  · V-REV 리밸런싱 모드 추가\n"
            f"  · DB reverse_day 컬럼 추가\n"
            f"  · 스케줄러 분리 (core / trade)\n"
            f"\n"
            f"🔧 <b>v2.0</b>  2026-05-10\n"
            f"  · 승승장군 알고리즘 7개 갭 수정\n"
            f"  · t_val 누적 회차 추적 구현\n"
            f"  · star_ratio / star_price 계산 엔진\n"
            f"  · 전반전·후반전·리버스 모드 분리\n"
            f"\n"
            f"🌱 <b>v1.0</b>  2026-05-01\n"
            f"  · 키움 REST API 연결 (mockapi)\n"
            f"  · 기본 무한매매 로직 초기 구현\n"
            f"  · python-telegram-bot v20+ 적용\n"
            f"\n"
            f"<b>[ 레퍼런스 ]</b>\n"
            f"  무한매매 원작: 라오어님\n"
            f"  AVWAP 엔진: 승승장군 V44~V79\n"
            f"  github.com/pipios4006-boop/\n"
            f"    KIS-API-Python-Trading-Bot-Example"
        )

        kb = [[
            InlineKeyboardButton("📋 통합 지시서", callback_data="CMD:sync"),
            InlineKeyboardButton("⚡️ AVWAP 현황", callback_data="AVWAP:REFRESH"),
        ]]
        await update.effective_message.reply_text(
            msg, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        await update.effective_message.reply_text(
            "📖 <b>명령어 가이드</b>\n"
            "\n"
            "🔍 <b>조회</b>\n"
            "/sync       — 통합 지시서 (T값·별값·주문계획)\n"
            "/record     — 장부 조회 (일자별 매매 내역)\n"
            "/balance    — 예수금 및 계좌 잔고\n"
            "/holdings   — 보유 종목 평가손익\n"
            "/report     — 당일 정산 리포트\n\n"
            "⚙️ <b>설정</b>\n"
            "/settlement — 설정 현황 및 파라미터 변경\n"
            "/ticker     — 종목 관리 (활성화/추가/제거)\n"
            "/mode       — INFINITE / V-REV 전환\n"
            "/seed       — 시드머니(할당금) 설정\n\n"
            "🎮 <b>제어</b>\n"
            "/pause      — 매매 일시 중지\n"
            "/resume     — 매매 재개\n"
            "/cancel     — 미체결 주문 전량 취소\n\n"
            "📊 <b>수동 주문 / 동기화</b>\n"
            "/buy        — 수동 매수 주문\n"
            "/sell       — 수동 매도 주문\n"
            "/sync_db    — 키움 잔고 → DB 강제 동기화\n"
            "\n"
            f"🤖 국내 ETF 무한매매 봇 {self.VERSION}",
            parse_mode="HTML"
        )
    # ==========================================================
    # 콜백 핸들러 (인라인 버튼)
    # ==========================================================
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if not self._is_admin(update):
            return
        data = query.data
        # ── 빠른 메뉴 (모두 query.edit_message_text 기반) ──────
        if data == "CMD:sync":
            await query.edit_message_text("🔄 지시서 작성 중...", parse_mode="HTML")
            await self._send_sync_report(query.message, context)
            return
        if data == "CMD:record":
            # ✅ 수정: query.edit_message_text 사용
            syms = self._get_active_symbols()
            kb   = [
                [InlineKeyboardButton(f"📋 {s['name']} ({s['code']})",
                                      callback_data=f"RECORD:{s['code']}")]
                for s in syms
            ]
            kb.append([InlineKeyboardButton("📋 전체 종목 조회", callback_data="RECORD:ALL")])
            await query.edit_message_text(
                "📊 <b>장부 조회</b>\n조회할 종목을 선택하세요:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return
        if data == "CMD:settlement":
            await self._show_settlement(None, query=query)
            return
        if data == "CMD:ticker":
            await self._show_ticker_menu(None, query=query)
            return
        if data == "CMD:balance":
            await query.edit_message_text("💰 잔고 조회 중...", parse_mode="HTML")
            try:
                b = await asyncio.to_thread(self.broker.get_balance)
                deposit      = b.get("deposit", 0)
                withdrawable = b.get("withdrawable", 0)
                eval_total   = b.get("eval_total", 0)
                eval_profit  = b.get("eval_profit", 0)
                profit_pct   = b.get("profit_pct", 0.0)
                sign = "+" if eval_profit >= 0 else ""
                await query.edit_message_text(
                    f"💰 <b>계좌 잔고 현황</b>\n"
                    f"📥 예수금:   {self._fmt_krw(deposit)}\n"
                    f"💳 출금가능: {self._fmt_krw(withdrawable)}\n"
                    f"📊 평가금액: {self._fmt_krw(eval_total)}\n"
                    f"💹 평가손익: {sign}{self._fmt_krw(eval_profit)} ({sign}{profit_pct:.2f}%)\n"
                    f"🕐 {datetime.datetime.now(KST).strftime('%H:%M:%S')} KST",
                    parse_mode="HTML"
                )
            except Exception as e:
                await query.edit_message_text(f"❌ 잔고 조회 실패: {e}")
            return
        if data == "CMD:sync_db":
            await query.edit_message_text("🔄 잔고 조회 중...", parse_mode="HTML")
            await self._show_syncdb_menu(None, query=query)
            return

        if data == "CMD:history":
            await self.cmd_history(update, context)
            return

        if data == "CMD:holdings":
            # ✅ 수정: query.edit_message_text 사용
            await query.edit_message_text("📈 보유 종목 조회 중...", parse_mode="HTML")
            try:
                hs = await asyncio.to_thread(self.broker.get_holdings)
                if not hs:
                    await query.edit_message_text(
                        "📭 현재 보유 중인 종목이 없습니다.", parse_mode="HTML"
                    )
                    return
                lines        = ["📈 <b>보유 종목 평가손익</b>", ""]
                total_profit = 0
                for h in hs:
                    profit = h.get("profit", 0)
                    pct    = h.get("profit_pct", 0.0)
                    icon   = "🟢" if profit >= 0 else "🔴"
                    sign   = "+" if profit >= 0 else ""
                    lines.append(
                        f"{icon} <b>{h.get('name','')}({h.get('code','')})</b>\n"
                        f"   {h.get('qty',0):,}주 | 평단 {self._fmt_krw(h.get('avg_price',0))} "
                        f"| 현재 {self._fmt_krw(h.get('current_price',0))}\n"
                        f"   손익: <b>{sign}{self._fmt_krw(profit)}</b> ({sign}{pct:.2f}%)"
                    )
                    total_profit += profit
                sign_t = "+" if total_profit >= 0 else ""
                lines += [
                    f"💼 총 평가손익: <b>{sign_t}{self._fmt_krw(total_profit)}</b>",
                    f"🕐 {datetime.datetime.now(KST).strftime('%H:%M:%S')} KST",
                ]
                kb = [[InlineKeyboardButton("🔄 새로고침", callback_data="CMD:holdings")]]
                await query.edit_message_text(
                    "\n".join(lines), parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
            except Exception as e:
                await query.edit_message_text(f"❌ 보유 종목 조회 실패: {e}")
            return
        # ── 장부 ─────────────────
        if data.startswith("RECORD:"):
            parts = data.split(":")
            if parts[1] == "ALL":
                # 전체 조회: 첫 종목은 edit, 나머지는 새 메시지
                syms = self._get_active_symbols()
                for idx, s in enumerate(syms):
                    if idx == 0:
                        await self._show_record(s["code"], query=query)
                    else:
                        await context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text="📋 조회 중...", parse_mode="HTML"
                        )
                        # 마지막 메시지를 msg_obj로 전달하기 위해 임시 처리
                        await self._show_record_to_chat(s["code"], context, update.effective_chat.id)
            elif parts[1] == "UPDATE" and len(parts) == 3:
                code = parts[2]
                await query.edit_message_text(f"🔄 {code} 장부 동기화 중...")
                # 실제 보유잔고 API 동기화
                try:
                    hs = await asyncio.to_thread(self.broker.get_holdings)
                    h  = next((x for x in hs if x["code"] == code), None)
                    if h:
                        self.db.upsert_position(
                            code=code, name=h["name"],
                            avg_price=h["avg_price"], total_qty=h["qty"],
                        )
                except Exception:
                    pass
                await self._show_record(code, query=query)
            else:
                code = parts[1]
                await self._show_record(code, query=query)
            return
        # ── 종목 관리 ─────────────
        if data.startswith("TICKER:"):
            parts = data.split(":")
            action = parts[1]
            if action == "TOGGLE" and len(parts) == 3:
                code = parts[2]
                syms = self._get_symbols()
                for s in syms:
                    if s["code"] == code:
                        cur = s.get("active", True)
                        # 보유 잔량 안전장치
                        if cur:
                            pos = self.db.get_position(code)
                            if pos and pos.get("total_qty", 0) > 0:
                                await query.answer(
                                    f"⚠️ {code} 보유 잔량({pos['total_qty']}주) 있음 — 청산 후 비활성화 가능",
                                    show_alert=True
                                )
                                return
                        s["active"] = not cur
                self._save_symbols(syms)
                await self._show_ticker_menu(None, query=query)
            elif action == "REMOVE" and len(parts) == 3:
                code = parts[2]
                pos  = self.db.get_position(code)
                if pos and pos.get("total_qty", 0) > 0:
                    await query.answer(
                        f"⚠️ {code} 보유 잔량 있음 — 청산 후 제거 가능",
                        show_alert=True
                    )
                    return
                syms = [s for s in self._get_symbols() if s["code"] != code]
                self._save_symbols(syms)
                await self._show_ticker_menu(None, query=query)
            elif action == "ADD":
                self._pending[update.effective_chat.id] = {
                    "action": "ADD_TICKER", "step": "code"
                }
                await query.edit_message_text(
                    "➕ <b>새 종목 추가</b>\n\n"
                    "추가할 종목 코드를 입력하세요.\n"
                    "(예: 122630)\n\n"
                    "취소: /cancel",
                    parse_mode="HTML"
                )
            elif action == "RESET":
                keyboard = [[
                    InlineKeyboardButton("✅ 기본 종목으로 복원", callback_data="TICKER:RESET:CONFIRM"),
                    InlineKeyboardButton("❌ 취소", callback_data="TICKER:RESET:ABORT"),
                ]]
                await query.edit_message_text(
                    "⚠️ 기본 종목 3개로 초기화합니다.\n현재 설정이 모두 초기화됩니다.",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            elif action == "RESET" and len(parts) == 3 and parts[2] == "CONFIRM":
                self._save_symbols(DEFAULT_SYMBOLS)
                await self._show_ticker_menu(None, query=query)
            return
        # ── 설정 변경 ─────────────
        if data.startswith("SETTLE:"):
            parts = data.split(":")
            action = parts[1]
            if action == "TOGGLE" and len(parts) == 3:
                code = parts[2]
                syms = self._get_symbols()
                for s in syms:
                    if s["code"] == code:
                        s["active"] = not s.get("active", True)
                self._save_symbols(syms)
                await self._show_settlement(None, query=query)
            elif action == "MODE" and len(parts) == 3:
                code = parts[2]
                syms = self._get_symbols()
                for s in syms:
                    if s["code"] == code:
                        s["mode"] = "VREV" if s.get("mode", "INFINITE") == "INFINITE" else "INFINITE"
                self._save_symbols(syms)
                await self._show_settlement(None, query=query)
            elif action == "AVWAP" and len(parts) >= 3:
                sub  = parts[2]   # TOGGLE or BUDGET
                code = parts[3] if len(parts) > 3 else ""
                syms = self._get_symbols()
                if sub == "TOGGLE":
                    for s in syms:
                        if s["code"] == code:
                            cur_budget = s.get("avwap_budget", 0)
                            if cur_budget > 0:
                                # OFF: 0으로
                                s["avwap_budget"] = 0
                            else:
                                # ON: 기본 100만원
                                s["avwap_budget"] = 1_000_000
                    self._save_symbols(syms)
                    # AVWAP 엔진 재초기화 알림
                    if hasattr(self, "avwap_engine") and self.avwap_engine:
                        avwap_syms = [s for s in syms
                                      if s.get("active") and s.get("avwap_budget", 0) > 0]
                        self.avwap_engine.init_symbols(avwap_syms)
                    await self._show_settlement(None, query=query)
                    return

                elif sub == "BUDGET":
                    sym = self._get_symbol(code)
                    if not sym:
                        return
                    self._pending[update.effective_chat.id] = {
                        "action": "SET_VALUE", "key": "avwap", "code": code
                    }
                    cur = sym.get("avwap_budget", 0)
                    await query.edit_message_text(
                        f"⚡️ <b>{sym['name']} AVWAP 예산 설정</b>\n\n"
                        f"현재: {self._fmt_krw(cur)}\n"
                        f"새 예산을 입력하세요 (원):\n"
                        f"예) <code>1000000</code> (100만원)\n\n"
                        f"0 입력 시 AVWAP 비활성화\n\n취소: /cancel",
                        parse_mode="HTML"
                    )
                    return

            elif action == "SET" and len(parts) == 4:
                # SETTLE:SET:{key}:{code}
                key  = parts[2]
                code = parts[3]
                sym  = self._get_symbol(code)
                if not sym or key not in SETTING_KEY_MAP:
                    return
                _, _, prompt, unit = SETTING_KEY_MAP[key]
                self._pending[update.effective_chat.id] = {
                    "action": "SET_VALUE", "key": key, "code": code
                }
                await query.edit_message_text(
                    f"⚙️ <b>{sym['name']} {key} 설정</b>\n\n"
                    f"새 값을 입력하세요: {prompt}\n"
                    f"단위: {unit}\n\n취소: /cancel",
                    parse_mode="HTML"
                )
            elif len(parts) == 2:
                # SETTLE:{code} — /sync 화면 ⚙️ 버튼 → 설정 화면 표시
                await self._show_settlement(None, query=query)
            return
        # ── 수동 주문 실행 콜백 ──────────────
        if data.startswith("MANORDER:"):
            parts  = data.split(":")
            action = parts[1]
            if action == "CANCEL":
                await query.edit_message_text("❌ 주문이 취소되었습니다.")
                return
            code  = parts[2]
            qty   = int(parts[3])
            price = int(parts[4])
            sym   = self._get_symbol(code)
            name  = sym["name"] if sym else code
            order_type = (self.broker.ORDER_MARKET if price == 0
                          else self.broker.ORDER_LIMIT)
            side_str = "🔴 매수" if action == "BUY" else "🔵 매도"
            await query.edit_message_text(f"⏳ {side_str} 주문 전송 중...", parse_mode="HTML")
            try:
                if action == "BUY":
                    res = await asyncio.to_thread(
                        self.broker.buy, code, qty, price, order_type
                    )
                    cur = price if price > 0 else await asyncio.to_thread(
                        self.broker.get_current_price, code)
                    pos = self.db.get_position(code) or {}
                    old_qty = int(pos.get("total_qty", 0))
                    old_avg = int(pos.get("avg_price", 0))
                    new_qty = old_qty + qty
                    new_avg = (old_avg*old_qty + cur*qty)//new_qty if new_qty > 0 else cur
                    self.db.upsert_position(
                        code=code, name=name,
                        avg_price=new_avg, total_qty=new_qty,
                        round_no=pos.get("round_no", 1),
                    )
                    self.db.record_trade(code=code, name=name, side="BUY",
                                         qty=qty, price=cur,
                                         order_no=res.get("order_no",""))
                    self.notifier.notify_buy(code, name, qty, cur, qty*cur)
                    await query.edit_message_text(
                        f"✅ <b>수동 매수 완료</b>\n\n"
                        f"종목: {name} ({code})\n수량: {qty:,}주\n"
                        f"단가: {self._fmt_krw(cur)}\n"
                        f"새 평단: {self._fmt_krw(new_avg)} ({new_qty:,}주)",
                        parse_mode="HTML")
                else:
                    res = await asyncio.to_thread(
                        self.broker.sell, code, qty, price, order_type
                    )
                    cur = price if price > 0 else await asyncio.to_thread(
                        self.broker.get_current_price, code)
                    pos = self.db.get_position(code) or {}
                    avg = int(pos.get("avg_price", 0))
                    remain = max(0, int(pos.get("total_qty", 0)) - qty)
                    profit = (cur - avg) * qty if avg > 0 else 0
                    pct    = (cur - avg) / avg * 100 if avg > 0 else 0.0
                    sign   = "+" if profit >= 0 else ""
                    self.db.upsert_position(
                        code=code, name=name,
                        avg_price=avg if remain > 0 else 0,
                        total_qty=remain,
                        round_no=pos.get("round_no",1) + (0 if remain > 0 else 1),
                    )
                    self.db.record_trade(code=code, name=name, side="SELL",
                                         qty=qty, price=cur,
                                         order_no=res.get("order_no",""),
                                         profit=profit, profit_pct=pct)
                    self.notifier.notify_sell(code, name, qty, cur, qty*cur, profit, pct)
                    await query.edit_message_text(
                        f"✅ <b>수동 매도 완료</b>\n\n"
                        f"종목: {name} ({code})\n수량: {qty:,}주\n"
                        f"단가: {self._fmt_krw(cur)}\n"
                        f"실현손익: <b>{sign}{self._fmt_krw(profit)}</b> ({sign}{pct:.2f}%)\n"
                        f"잔량: {remain:,}주",
                        parse_mode="HTML")
            except Exception as e:
                log.exception("[TG] 수동 주문 실패")
                await query.edit_message_text(f"❌ 주문 실패: {e}")
            return
        if data.startswith("MANBUY:SELECT:"):
            code = data.split(":")[2]
            sym  = self._get_symbol(code)
            name = sym["name"] if sym else code
            self._pending[update.effective_chat.id] = {
                "action": "MANUAL_BUY", "code": code
            }
            try:
                cur = await asyncio.to_thread(self.broker.get_current_price, code)
            except Exception:
                cur = 0
            await query.edit_message_text(
                f"🔴 <b>{name} 수동 매수</b>\n\n"
                f"현재가: {self._fmt_krw(cur)}\n\n"
                f"수량과 가격을 입력하세요 (시장가는 가격 생략):\n"
                f"예) <code>5 15000</code> 또는 <code>5</code>\n\n취소: /cancel",
                parse_mode="HTML")
            return
        if data.startswith("MANSELL:SELECT:"):
            code = data.split(":")[2]
            sym  = self._get_symbol(code)
            name = sym["name"] if sym else code
            pos  = self.db.get_position(code) or {}
            hold_qty = int(pos.get("total_qty", 0))
            self._pending[update.effective_chat.id] = {
                "action": "MANUAL_SELL", "code": code, "max_qty": hold_qty
            }
            try:
                cur = await asyncio.to_thread(self.broker.get_current_price, code)
            except Exception:
                cur = 0
            await query.edit_message_text(
                f"🔵 <b>{name} 수동 매도</b>\n\n"
                f"보유: {hold_qty:,}주 | 현재가: {self._fmt_krw(cur)}\n\n"
                f"수량과 가격을 입력하세요 (전량은 0 입력):\n"
                f"예) <code>5 16000</code> 또는 <code>0</code>\n\n취소: /cancel",
                parse_mode="HTML")
            return
        if data.startswith("HISTORY:"):
            code = data.split(":")[1]
            logs = self.db.get_cycle_log(code, limit=20)
            stats = self.db.get_cycle_stats(code)
            sym  = self._get_symbol(code)
            name = sym["name"] if sym else code

            if not logs:
                await query.edit_message_text(
                    f"🏆 {name}\n아직 졸업 기록이 없습니다.",
                    parse_mode="HTML"
                )
                return

            sign_t = "+" if stats.get("total_profit", 0) >= 0 else ""
            lines  = [
                f"🏆 <b>{name} 졸업 기록</b>",
                f"",
                f"총 {stats['total']}회 | 승률 {stats['win_rate']:.1f}%",
                f"총 수익: <b>{sign_t}{self._fmt_krw(stats['total_profit'])}</b>",
                f"평균: {stats['avg_pct']:+.2f}% | "
                f"최고: {stats['best_pct']:+.2f}%",
                f"",
                f"<b>회차별 기록</b>",
            ]
            for log in logs:
                pct    = log["profit_pct"]
                profit = log["profit"]
                sign   = "+" if profit >= 0 else ""
                icon   = "🏅" if pct >= 5 else ("✅" if pct >= 0 else "⚠️")
                lines.append(
                    f"{icon} {log['round_no']}회차  "
                    f"{sign}{pct:.2f}%  {sign}{self._fmt_krw(profit)}  "
                    f"T:{log['final_t_val']:.2f}  {log['end_date']}"
                )

            kb = [[InlineKeyboardButton("◀ 전체 보기", callback_data="CMD:history")]]
            await query.edit_message_text(
                "\n".join(lines), parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return

        if data.startswith("AVWAP:"):
            parts  = data.split(":")
            action = parts[1]

            if action == "REFRESH":
                # avwap_engine 없으면 안내 메시지 표시 (무반응 방지)
                if not hasattr(self, "avwap_engine") or not self.avwap_engine:
                    kb = [[InlineKeyboardButton(
                        "⚙️ 설정에서 AVWAP 켜기",
                        callback_data="CMD:settlement"
                    )]]
                    await query.edit_message_text(
                        "⚡️ <b>AVWAP 엔진</b>\n\n"
                        "현재 비활성 상태입니다.\n\n"
                        "활성화 방법:\n"
                        "1️⃣ /settlement → 종목 선택\n"
                        "2️⃣ <code>💤 AVWAP OFF</code> 버튼 클릭\n"
                        "3️⃣ 예산 확인 후 자동 활성화\n\n"
                        "또는 data/config.json에서\n"
                        "<code>avwap_budget: 1000000</code> 설정",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(kb)
                    )
                    return

                txt = self.avwap_engine.get_status_text()
                kb  = [[
                    InlineKeyboardButton("🔄 새로고침", callback_data="AVWAP:REFRESH")
                ]]
                # 종목별 상세 버튼 추가
                for code in list(self.avwap_engine.states.keys()):
                    st = self.avwap_engine.states[code]
                    kb.append([InlineKeyboardButton(
                        f"🔍 {st.name} 상세",
                        callback_data=f"AVWAP:STATUS:{code}"
                    )])
                await query.edit_message_text(
                    txt, parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
                return

            if action == "STATUS" and len(parts) == 3:
                code = parts[2]
                if not hasattr(self, "avwap_engine") or not self.avwap_engine:
                    await query.answer("AVWAP 엔진이 비활성 상태입니다.", show_alert=True)
                    return
                txt = self.avwap_engine.get_status_text(code)
                kb  = [[
                    InlineKeyboardButton("◀ 전체 보기", callback_data="AVWAP:REFRESH"),
                    InlineKeyboardButton("🔄 새로고침", callback_data=f"AVWAP:STATUS:{code}"),
                ]]
                await query.edit_message_text(
                    txt, parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
                return

        if data.startswith("SYNCDB:"):
            action = data.split(":")[1]

            if action in ("VIEW", "RUN"):
                # 현황 비교 화면
                await query.edit_message_text(
                    "🔄 잔고 조회 중...", parse_mode="HTML")
                await self._show_syncdb_menu(None, query=query)

            elif action == "EXEC":
                # 동기화 실행
                await query.edit_message_text(
                    "⏳ 키움 → DB 동기화 실행 중...", parse_mode="HTML")
                await self._do_sync_db(query.message)

            elif action == "CLOSE":
                await query.edit_message_text("✅ DB 동기화 화면을 닫았습니다.")

            return
        # ── 미체결 취소 ───────────
        if data == "CANCEL:CONFIRM":
            await query.edit_message_text("⏳ 미체결 취소 중...")
            try:
                r = await asyncio.to_thread(self.broker.cancel_all_orders)
                await query.edit_message_text(
                    f"✅ <b>미체결 {r.get('cancelled',0)}건 취소 완료</b>",
                    parse_mode="HTML"
                )
            except Exception as e:
                await query.edit_message_text(f"❌ 취소 실패: {e}")
        # ── 수동 주문 UI 단계별 라우팅 (BUY: / SEL:) ─────────
        if data in ("BUY:START", "SEL:START"):
            side = "BUY" if data == "BUY:START" else "SELL"
            await self._show_trade_ticker(None, side, query=query)
            return

        if data.startswith("BUY:") or data.startswith("SEL:"):
            parts = data.split(":")
            pre   = parts[0]
            step  = parts[1]
            side  = "BUY" if pre == "BUY" else "SELL"
            if step == "BACK":
                await self._show_trade_ticker(None, side, query=query)
                return
            code = parts[2] if len(parts) > 2 else ""
            if step == "T":
                await self._show_trade_qty(query, side, code)
                return
            qty_raw = parts[3] if len(parts) > 3 else "0"
            if step == "Q":
                if qty_raw == "M":
                    self._pending[update.effective_chat.id] = {
                        "action": ("MANUAL_BUY" if side == "BUY"
                                   else "MANUAL_SELL"),
                        "code": code, "step": "qty",
                    }
                    await query.edit_message_text(
                        ("🔴" if side=="BUY" else "🔵") +
                        " 수량을 입력하세요:\n예) <code>7</code>\n\n취소: /cancel",
                        parse_mode="HTML")
                    return
                await self._show_trade_price(query, side, code, int(qty_raw))
                return
            qty = int(qty_raw) if qty_raw.isdigit() else 0
            price_raw = parts[4] if len(parts) > 4 else "0"
            if step == "P":
                if price_raw == "M":
                    self._pending[update.effective_chat.id] = {
                        "action": ("MANUAL_BUY" if side == "BUY"
                                   else "MANUAL_SELL"),
                        "code": code, "step": "price", "qty": qty,
                    }
                    await query.edit_message_text(
                        ("🔴" if side=="BUY" else "🔵") +
                        " 가격을 입력하세요 (0=시장가):\n예) <code>15250</code>\n\n취소: /cancel",
                        parse_mode="HTML")
                    return
                await self._show_trade_confirm(
                    query, side, code, qty, int(price_raw))
                return

        if data == "TRADE:CANCEL":
            await query.edit_message_text("❌ 주문이 취소되었습니다.")
            return

        elif data == "CANCEL:ABORT":
            await query.edit_message_text("취소 작업이 중단되었습니다.")
        elif data == "NOOP":
            pass
    # ==========================================================
    # 텍스트 메시지 핸들러 (설정 입력 상태 머신)
    # ==========================================================
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        uid   = update.effective_chat.id
        text  = update.effective_message.text.strip()
        state = self._pending.get(uid)
        # ── 종목 추가 상태 머신 ──────────
        if state and state.get("action") == "ADD_TICKER":
            step = state.get("step")
            if step == "code":
                code = text.strip()
                if not code.isdigit() or len(code) != 6:
                    await update.effective_message.reply_text(
                        "❌ 종목 코드는 6자리 숫자입니다. (예: 122630)\n다시 입력하세요:"
                    )
                    return
                if self._get_symbol(code):
                    await update.effective_message.reply_text(
                        f"⚠️ {code}는 이미 등록된 종목입니다."
                    )
                    del self._pending[uid]
                    return
                state["code"] = code
                state["step"] = "name"
                await update.effective_message.reply_text(
                    f"✅ 코드: {code}\n\n종목 이름을 입력하세요:\n(예: KODEX 레버리지)"
                )
                return
            if step == "name":
                state["name"] = text
                state["step"] = "alloc"
                await update.effective_message.reply_text(
                    f"✅ 종목명: {text}\n\n할당금액(원)을 입력하세요:\n(예: 3000000)"
                )
                return
            if step == "alloc":
                try:
                    alloc = int(text.replace(",", "").replace("원", ""))
                except ValueError:
                    await update.effective_message.reply_text("❌ 숫자만 입력하세요. (예: 3000000)")
                    return
                code  = state["code"]
                name  = state["name"]
                new_s = {
                    "code": code, "name": name,
                    "mode": "INFINITE", "active": True,
                    "allocation_krw": alloc,
                    "split_count": 10,
                    "target_profit_pct": 5.0,
                    "vrev_band_pct": 3.0,
                    "daily_buy_limit_krw": alloc // 10,
                }
                syms = self._get_symbols()
                syms.append(new_s)
                self._save_symbols(syms)
                del self._pending[uid]
                await update.effective_message.reply_text(
                    f"✅ <b>{name}({code}) 추가 완료!</b>\n"
                    f"할당금: {self._fmt_krw(alloc)}\n"
                    f"분할: 10회 | 목표: 5.0%\n\n"
                    f"/settlement 에서 세부 설정 변경 가능합니다.",
                    parse_mode="HTML"
                )
                return
        # ── 수동 매수 텍스트 입력 ──────────────
        if state and state.get("action") == "MANUAL_BUY":
            code  = state["code"]
            parts = text.strip().split()
            try:
                qty   = int(parts[0])
                price = int(parts[1]) if len(parts) >= 2 else 0
            except (ValueError, IndexError):
                await update.effective_message.reply_text(
                    "❌ 예) <code>5 15000</code> 또는 <code>5</code>",
                    parse_mode="HTML")
                return
            del self._pending[uid]
            await self._execute_manual_buy(update.effective_message, code, qty, price)
            return
        # ── 수동 매도 텍스트 입력 ──────────────
        if state and state.get("action") == "MANUAL_SELL":
            code    = state["code"]
            max_qty = state.get("max_qty", 0)
            parts   = text.strip().split()
            try:
                qty   = int(parts[0])
                price = int(parts[1]) if len(parts) >= 2 else 0
            except (ValueError, IndexError):
                await update.effective_message.reply_text(
                    "❌ 예) <code>5 16000</code> 또는 <code>0</code>(전량)",
                    parse_mode="HTML")
                return
            if qty == 0:
                qty = max_qty
            del self._pending[uid]
            await self._execute_manual_sell(update.effective_message, code, qty, price)
            return
        # ── 설정값 입력 상태 ────────────
        if state and state.get("action") == "SET_VALUE":
            key  = state["key"]
            code = state["code"]
            cfg_key, type_fn, _, unit = SETTING_KEY_MAP[key]
            try:
                val = type_fn(text.replace(",", "").replace(unit, "").strip())
            except ValueError:
                await update.effective_message.reply_text(f"❌ 올바른 숫자를 입력하세요. 단위: {unit}")
                return
            syms = self._get_symbols()
            sym_name = code
            for s in syms:
                if s["code"] == code:
                    s[cfg_key] = val
                    sym_name   = s["name"]
                    break
            self._save_symbols(syms)
            del self._pending[uid]
            # AVWAP 예산 변경 시 엔진 실시간 반영
            if key == "avwap" and hasattr(self, "avwap_engine") and self.avwap_engine:
                avwap_syms = [s for s in syms
                              if s.get("active") and s.get("avwap_budget", 0) > 0]
                self.avwap_engine.init_symbols(avwap_syms)
                extra = "  (활성화)" if val > 0 else "  (비활성화)"
            else:
                extra = ""
            await update.effective_message.reply_text(
                f"✅ <b>{sym_name} {key} 설정 완료</b>\n"
                f"새 값: {val:,}{unit}{extra}",
                parse_mode="HTML"
            )
            return
        # ── 텍스트 키워드 라우팅 ──────────
        routing = {
            "지시서": self.cmd_sync,   "sync": self.cmd_sync,
            "장부":   self.cmd_record, "record": self.cmd_record,
            "잔고":   self.cmd_balance,"balance": self.cmd_balance,
            "보유":   self.cmd_holdings,
            "설정":   self.cmd_settlement,
            "종목":   self.cmd_ticker,
            "도움말": self.cmd_help,
            "중지":   self.cmd_pause,
            "재개":   self.cmd_resume,
        }
        for kw, handler in routing.items():
            if kw in text.lower():
                return await handler(update, context)
        await update.effective_message.reply_text(
            "❓ 알 수 없는 명령입니다. /help 로 명령어 목록을 확인하세요."
        )