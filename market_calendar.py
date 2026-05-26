# ==========================================================
# [market_calendar.py] KRX 거래일/휴장일 관리 v3.1
# BUG FIX: __init__ config 파라미터 선택적으로 변경
# ==========================================================
import datetime
import json
import os
import requests
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

# ── 2025~2027 국내 공휴일 오프라인 테이블 ──────────────────
KRX_HOLIDAYS_OFFLINE = {
    # 2025년
    "20250101", "20250128", "20250129", "20250130",
    "20250301", "20250505", "20250506", "20250606",
    "20250815", "20251003", "20251009", "20251231",
    # 2026년
    "20260101", "20260216", "20260217", "20260218",
    "20260301", "20260505", "20260606",
    "20260815", "20261001", "20261002", "20261009",
}


class MarketCalendar:
    """KRX 거래일 및 매매시간 관리 클래스."""

    HOLIDAYS_CACHE_FILE = "data/krx_holidays.json"

    # BUG FIX: config=None 으로 선택적 파라미터화
    def __init__(self, config: dict = None):
        self.schedule_cfg = (config or {}).get("SCHEDULE", {})
        self.holidays = set(KRX_HOLIDAYS_OFFLINE)
        self._load_cached_holidays()

    def _load_cached_holidays(self) -> None:
        if os.path.exists(self.HOLIDAYS_CACHE_FILE):
            try:
                with open(self.HOLIDAYS_CACHE_FILE, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                self.holidays.update(cached.get("holidays", []))
            except Exception as e:
                print(f"⚠️ [Calendar] 공휴일 캐시 로드 실패: {e}")

    def is_trading_day(self, dt: datetime.datetime = None) -> bool:
        """오늘(또는 지정일)이 거래일인지 반환."""
        if dt is None:
            dt = datetime.datetime.now(KST)
        date_str = dt.strftime("%Y%m%d")
        if dt.weekday() >= 5:
            return False
        if date_str in self.holidays:
            return False
        return True

    def get_next_trading_day(self) -> datetime.date:
        dt = datetime.datetime.now(KST) + datetime.timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(dt):
                return dt.date()
            dt += datetime.timedelta(days=1)
        raise RuntimeError("30일 내 거래일을 찾지 못했습니다.")

    def _parse_time(self, time_str: str) -> datetime.time:
        h, m = map(int, time_str.split(":"))
        return datetime.time(h, m)

    def get_schedule(self, config: dict = None) -> dict:
        sch = (config or {}).get("SCHEDULE", self.schedule_cfg)
        return {
            "start":           self._parse_time(sch.get("START_TIME", "09:00")),
            "end":             self._parse_time(sch.get("END_TIME", "15:20")),
            "pre_close_start": self._parse_time(sch.get("PRE_CLOSE_START", "15:10")),
            "pre_close_end":   self._parse_time(sch.get("PRE_CLOSE_END", "15:20")),
            "auction_start":   self._parse_time(sch.get("CLOSING_AUCTION_START", "15:20")),
            "auction_end":     self._parse_time(sch.get("CLOSING_AUCTION_END", "15:30")),
            "morning_check":   self._parse_time(sch.get("MORNING_CHECK_TIME", "08:50")),
        }

    def is_market_open(self, config: dict = None, dt: datetime.datetime = None) -> bool:
        if dt is None:
            dt = datetime.datetime.now(KST)
        if not self.is_trading_day(dt):
            return False
        sch = self.get_schedule(config)
        cur = dt.time().replace(second=0, microsecond=0)
        return sch["start"] <= cur < sch["end"]

    def is_pre_close_window(self, config: dict = None, dt: datetime.datetime = None) -> bool:
        if dt is None:
            dt = datetime.datetime.now(KST)
        if not self.is_trading_day(dt):
            return False
        sch = self.get_schedule(config)
        cur = dt.time().replace(second=0, microsecond=0)
        return sch["pre_close_start"] <= cur < sch["pre_close_end"]

    def is_closing_auction(self, config: dict = None, dt: datetime.datetime = None) -> bool:
        if dt is None:
            dt = datetime.datetime.now(KST)
        if not self.is_trading_day(dt):
            return False
        sch = self.get_schedule(config)
        cur = dt.time().replace(second=0, microsecond=0)
        return sch["auction_start"] <= cur < sch["auction_end"]

    def current_kst(self) -> datetime.datetime:
        return datetime.datetime.now(KST)
