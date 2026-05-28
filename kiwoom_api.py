# ==============================================================
# [kiwoom_api.py] 키움증권 REST API 브로커 클라이언트 v3.3
#
# BUG FIX v3.3: 실제 키움 REST API 명세로 전면 교정
#   - BASE_URL:
#       MOCK: https://mockapi.kiwoom.com
#       REAL: https://api.kiwoom.com
#   - 토큰 엔드포인트: /oauth2/token
#   - API 경로: /api/dostk/acnt, /api/dostk/stkinfo, /api/dostk/ordr
#   - 헤더 방식: "api-id" 헤더 (TR ID 별도 헤더)
#   - 응답 return_code 기준 에러 처리
#   - 실제 응답 필드명 교정 (IAMYWKIM 레퍼런스 v1.3 기준)
# ==============================================================
import os
import json
import time
import logging
import threading
import requests
import datetime
from pathlib import Path
from collections import deque
from dotenv import load_dotenv

from config_adapter import ConfigAdapter

load_dotenv()
log = logging.getLogger(__name__)

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# ------------------------------------------------------------------
# 실제 키움증권 REST API URL (레퍼런스 broker.py 확인)
# ------------------------------------------------------------------
BASE_URL_MOCK = "https://mockapi.kiwoom.com"   # 모의투자
BASE_URL_REAL = "https://api.kiwoom.com"       # 실전투자

# 호가 단위 테이블
TICK_TABLE = [
    (     2_000,    1),
    (     5_000,    5),
    (    20_000,   10),
    (    50_000,   50),
    (   200_000,  100),
    (   500_000,  500),
    (float("inf"), 1000),
]


def get_tick_size(price: int) -> int:
    for threshold, tick in TICK_TABLE:
        if price < threshold:
            return tick
    return 1000


def round_to_tick(price: float) -> int:
    p = int(price)
    tick = get_tick_size(p)
    return (p // tick) * tick


@staticmethod
def _to_int(s, default: int = 0) -> int:
    """부호 있는 숫자 문자열 → 절대값 정수 ('+294250' → 294250)"""
    try:
        return abs(int((str(s or "0")).lstrip("0+-") or "0"))
    except (ValueError, TypeError):
        return default


# Rate Limiter (초당 4회)
class _RateLimiter:
    def __init__(self, max_calls=4, period=1.0):
        self._max_calls = max_calls
        self._period    = period
        self._min_interval = period / max_calls
        self._calls: deque = deque()
        self._lock  = threading.Lock()
        self._last  = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            since = now - self._last
            if since < self._min_interval:
                time.sleep(self._min_interval - since)
                now = time.monotonic()
            while self._calls and now - self._calls[0] >= self._period:
                self._calls.popleft()
            if len(self._calls) >= self._max_calls:
                s = self._period - (now - self._calls[0]) + 0.05
                if s > 0:
                    time.sleep(s)
                    now = time.monotonic()
                while self._calls and now - self._calls[0] >= self._period:
                    self._calls.popleft()
            self._last = time.monotonic()
            self._calls.append(self._last)


_rate_limiter = _RateLimiter()


class KiwoomBroker:
    """키움증권 REST API 통신 클라이언트 (실제 명세 기반)."""

    ORDER_LIMIT   = "0"   # 지정가
    ORDER_MARKET  = "3"   # 시장가
    ORDER_AUCTION = "5"   # 장마감 동시호가

    def __init__(self):
        load_dotenv()
        self.cfg = ConfigAdapter()

        mode = os.getenv("TRADE_MODE", "MOCK").upper()
        if mode == "REAL":
            self._app_key    = os.getenv("KIWOOM_APP_KEY", "")
            self._app_secret = os.getenv("KIWOOM_SECRET_KEY", "")
            self._account_no = os.getenv("KIWOOM_ACCOUNT_NO", "")
            self._base_url   = BASE_URL_REAL
            self._mock       = False
        else:
            self._app_key    = os.getenv("KIWOOM_APP_KEY_MOCK", "")
            self._app_secret = os.getenv("KIWOOM_SECRET_KEY_MOCK", "")
            self._account_no = os.getenv("KIWOOM_ACCOUNT_NO_MOCK", "")
            self._base_url   = BASE_URL_MOCK
            self._mock       = True

        self._token: str = ""
        self._token_exp: datetime.datetime = datetime.datetime.min
        self._token_cache = DATA_DIR / f"token_{'mock' if self._mock else 'real'}.json"
        self._lock = threading.Lock()

        self.cfg.set("TRADE_MODE", "MOCK" if self._mock else "REAL")
        log.info(
            f"[Broker] 초기화 — 모드:{'MOCK' if self._mock else 'REAL'} "
            f"계좌:{self._account_no} URL:{self._base_url}"
        )

    # ----------------------------------------------------------
    # 토큰 관리 (au10001)
    # ----------------------------------------------------------
    def _get_token(self, force: bool = False) -> str:
        with self._lock:
            now = datetime.datetime.now()

            # 캐시 파일 우선 확인
            if not force and self._token_cache.exists():
                try:
                    cache = json.loads(self._token_cache.read_text(encoding="utf-8"))
                    exp   = datetime.datetime.strptime(cache["expires_dt"], "%Y%m%d%H%M%S")
                    if now < exp - datetime.timedelta(hours=1):
                        self._token     = cache["token"]
                        self._token_exp = exp
                        return self._token
                except Exception as e:
                    log.warning(f"[Broker] 캐시 읽기 실패: {e}")

            log.info("[Broker] 접근토큰 발급 요청...")
            resp = requests.post(
                f"{self._base_url}/oauth2/token",
                headers={"Content-Type": "application/json;charset=UTF-8"},
                json={
                    "grant_type": "client_credentials",
                    "appkey":     self._app_key,
                    "secretkey":  self._app_secret,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("return_code") != 0:
                raise RuntimeError(f"토큰 발급 실패: {data.get('return_msg')}")

            self._token     = data["token"]
            expires_dt      = data["expires_dt"]   # "20241107083713"
            self._token_exp = datetime.datetime.strptime(expires_dt, "%Y%m%d%H%M%S")

            # 캐시 저장
            tmp = self._token_cache.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"token": self._token, "expires_dt": expires_dt},
                           ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._token_cache)

            log.info(
                f"[Broker] 토큰 발급 성공 — "
                f"만료: {self._token_exp.strftime('%Y-%m-%d %H:%M')}"
            )
            return self._token

    def _headers(self, api_id: str) -> dict:
        return {
            "Content-Type":  "application/json;charset=UTF-8",
            "authorization": f"Bearer {self._get_token()}",
            "api-id":        api_id,           # 키움 헤더 방식
        }

    def _post(self, api_id: str, path: str, body: dict,
              retry: bool = True) -> dict:
        """POST 공통 메서드 — Rate Limiter + 401/429 자동 재시도."""
        _rate_limiter.wait()
        url = f"{self._base_url}{path}"
        try:
            resp = requests.post(
                url, headers=self._headers(api_id), json=body, timeout=15
            )
            if resp.status_code == 401 and retry:
                log.warning("[Broker] 401 → 토큰 재발급 후 재시도")
                self._get_token(force=True)
                return self._post(api_id, path, body, retry=False)

            if resp.status_code == 429:
                for attempt in range(1, 3):
                    wait_s = 5 * attempt
                    log.warning(f"[Broker] 429 → {wait_s}초 대기 후 재시도 ({attempt}/2)")
                    time.sleep(wait_s)
                    _rate_limiter.wait()
                    resp = requests.post(
                        url, headers=self._headers(api_id), json=body, timeout=15
                    )
                    if resp.status_code != 429:
                        break

            resp.raise_for_status()
            data = resp.json()
            if data.get("return_code") not in (0, None):
                log.error(f"[Broker] API 오류 [{api_id}]: {data.get('return_msg')}")
            return data

        except requests.Timeout:
            log.error(f"[Broker] 타임아웃 [{api_id}]")
            raise
        except requests.HTTPError as e:
            log.error(f"[Broker] HTTP 오류 [{api_id}]: {e.response.status_code}")
            raise

    # ----------------------------------------------------------
    # 연결 확인 (ping) — 비치명적
    # ----------------------------------------------------------
    def ping(self) -> bool:
        try:
            token = self._get_token()
            acct  = self.get_account_no()
            print(f"✅ 키움 REST API 연결 성공! [{'MOCK' if self._mock else 'REAL'} 모드]")
            print(f"   토큰 앞 20자: {token[:20]}...")
            print(f"   토큰 만료:    {self._token_exp.strftime('%Y-%m-%d %H:%M')}")
            print(f"   계좌번호:     {acct}")
            return True
        except Exception as e:
            log.error(f"[Broker] API 연결 실패: {e}")
            print(f"⚠️  키움 REST API 연결 실패 (봇은 계속 실행)")
            print(f"   URL: {self._base_url}")
            print(f"   오류: {e}")
            return False

    # ----------------------------------------------------------
    # 계좌번호 조회 (ka00001)
    # ----------------------------------------------------------
    def get_account_no(self) -> str:
        data = self._post("ka00001", "/api/dostk/acnt", {})
        return data.get("acctNo", self._account_no)

    # ----------------------------------------------------------
    # 잔고 조회 (kt00018) — get_balance() 인터페이스 유지
    # ----------------------------------------------------------
    def get_balance(self) -> dict:
        """
        kt00001 주식잔고2 — 예수금/주문가능금액 조회
        반환: {deposit, withdrawable, eval_total, eval_profit, profit_pct}

        kt00018 대신 kt00001 사용 이유:
          kt00018은 dmst_stex_tp 파라미터 오류(501307)로 실전 계좌 조회 불가
          kt00001은 동일 파라미터로 정상 조회됨 (실전 검증 완료)

        kt00001 주요 응답 필드:
          entr         = 예수금
          ord_alow_amt = 주문가능금액
          elwdpst_evlta= 평가금액 합계
        """
        data = self._post(
            "kt00001", "/api/dostk/acnt",
            {"qry_tp": "1"},
        )

        def ti(s):
            try:
                return abs(int((str(s or "0")).lstrip("0+-") or "0"))
            except Exception:
                return 0

        deposit      = ti(data.get("entr",          "0"))  # 예수금
        withdrawable = ti(data.get("ord_alow_amt",  "0"))  # 주문가능금액
        eval_total   = ti(data.get("elwdpst_evlta", "0"))  # 평가금액

        # 보유종목 평가손익 합산
        eval_profit = 0
        for item in data.get("stk_entr_prst", []):
            try:
                eval_profit += int(str(item.get("evlt_pl_amt", "0")).lstrip("0+-") or "0")
            except Exception:
                pass

        profit_pct = (eval_profit / eval_total * 100) if eval_total > 0 else 0.0

        log.debug(
            f"[Broker] 잔고 — 예수금:{deposit:,} "
            f"주문가능:{withdrawable:,} 평가:{eval_total:,}"
        )
        return {
            "deposit":      deposit,
            "withdrawable": withdrawable,
            "eval_total":   eval_total,
            "eval_profit":  eval_profit,
            "profit_pct":   round(profit_pct, 2),
        }

    # ----------------------------------------------------------
    # 보유 종목 조회 (kt00018) — get_holdings() 인터페이스 유지
    # ----------------------------------------------------------
    def get_holdings(self) -> list:
        """
        kt00001 보유 종목 리스트 (kt00018 대체)
        반환: [{code, name, qty, avg_price, current_price, profit, profit_pct}]

        kt00001 응답의 stk_entr_prst 배열에서 보유종목 파싱
        """
        data = self._post(
            "kt00001", "/api/dostk/acnt",
            {"qry_tp": "1"},
        )

        def ti(s):
            try:
                return abs(int((str(s or "0")).lstrip("0+-") or "0"))
            except Exception:
                return 0

        holdings = []
        for item in data.get("stk_entr_prst", []):
            qty = ti(item.get("rmnd_qty", "0"))
            if qty == 0:
                continue
            avg_price  = ti(item.get("pur_pric",  "0"))
            curr_price = ti(item.get("cur_prc",   "0"))
            eval_amt   = ti(item.get("evlt_amt",  "0"))
            profit     = eval_amt - avg_price * qty

            try:
                profit_pct = float(item.get("prft_rt", "0"))
            except Exception:
                profit_pct = 0.0

            holdings.append({
                "code":          item.get("stk_cd", "").lstrip("A"),
                "name":          item.get("stk_nm", ""),
                "qty":           qty,
                "avg_price":     avg_price,
                "current_price": curr_price,
                "eval_amount":   eval_amt,
                "profit":        profit,
                "profit_pct":    profit_pct,
            })
        return holdings

    # ----------------------------------------------------------
    # 현재가 조회 (ka10001)
    # ----------------------------------------------------------
    def get_current_price(self, code: str) -> int:
        try:
            data = self._post("ka10001", "/api/dostk/stkinfo", {"stk_cd": code})
            def ti(s):
                try:
                    return abs(int((str(s or "0")).lstrip("0+-") or "0"))
                except Exception:
                    return 0
            price = ti(data.get("cur_prc", "0"))
            log.info(f"[Broker] {code} 현재가: {price:,}원")
            return price
        except Exception as e:
            log.error(f"[Broker] 현재가 조회 실패 {code}: {e}")
            return 0

    # ----------------------------------------------------------
    # 매수 주문 (kt10000)
    # ----------------------------------------------------------
    def buy(self, code: str, qty: int, price: int = 0,
            order_type: str = None) -> dict:
        order_type = order_type or self.ORDER_LIMIT
        if order_type == self.ORDER_LIMIT and price > 0:
            price = round_to_tick(price)
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd":       code,
            "ord_qty":      str(qty),
            "ord_uv":       str(price) if price > 0 else "",
            "trde_tp":      order_type,
            "cond_uv":      "",
        }
        data     = self._post("kt10000", "/api/dostk/ordr", body)
        success  = data.get("return_code") == 0
        order_no = data.get("ord_no", "")
        log.info(f"[Broker] 매수 {'성공' if success else '실패'}: {code} {qty}주 @{price:,}원 → {order_no}")
        return {"order_no": order_no, "code": code, "qty": qty, "price": price,
                "success": success}

    # ----------------------------------------------------------
    # 매도 주문 (kt10001)
    # ----------------------------------------------------------
    def sell(self, code: str, qty: int, price: int = 0,
             order_type: str = None) -> dict:
        order_type = order_type or self.ORDER_LIMIT
        if order_type == self.ORDER_LIMIT and price > 0:
            price = round_to_tick(price)
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd":       code,
            "ord_qty":      str(qty),
            "ord_uv":       str(price) if price > 0 else "",
            "trde_tp":      order_type,
            "cond_uv":      "",
        }
        data     = self._post("kt10001", "/api/dostk/ordr", body)
        success  = data.get("return_code") == 0
        order_no = data.get("ord_no", "")
        log.info(f"[Broker] 매도 {'성공' if success else '실패'}: {code} {qty}주 @{price:,}원 → {order_no}")
        return {"order_no": order_no, "code": code, "qty": qty, "price": price,
                "success": success}

    # ----------------------------------------------------------
    # 미체결 / 취소
    # ----------------------------------------------------------
    def get_open_orders(self) -> list:
        try:
            data   = self._post("kt00008", "/api/dostk/acnt",
                                {"qry_tp": "0", "stk_cd": ""})
            orders = []
            for item in data.get("oso_ordr_remn", []):
                qty    = int(item.get("ord_qty",      "0") or "0")
                filled = int(item.get("tot_ccls_qty", "0") or "0")
                remain = qty - filled
                if remain > 0:
                    orders.append({
                        "order_no": item.get("ord_no", ""),
                        "code":     item.get("stk_cd", "").lstrip("A"),
                        "side":     "BUY" if item.get("trde_tp") in ("0","1","2","3") else "SELL",
                        "qty":      qty,
                        "remain":   remain,
                        "price":    int(item.get("ord_uv", "0") or "0"),
                    })
            return orders
        except Exception as e:
            log.error(f"[Broker] 미체결 조회 실패: {e}")
            return []

    def cancel_order(self, order_no: str, code: str, qty: int) -> bool:
        try:
            data = self._post(
                "kt10002", "/api/dostk/ordr",
                {"orig_ord_no": order_no, "stk_cd": code,
                 "ord_qty": str(qty), "trde_tp": "0", "cond_uv": ""},
            )
            ok = data.get("return_code") == 0
            log.info(f"[Broker] 주문취소 {'성공' if ok else '실패'}: {order_no}")
            return ok
        except Exception as e:
            log.error(f"[Broker] 주문 취소 실패 {order_no}: {e}")
            return False


    # ----------------------------------------------------------
    # 주식 기본정보 조회 (ka10001) — 고가/저가/전일종가 포함
    # ----------------------------------------------------------
    def get_stock_info(self, code: str) -> dict:
        """
        ka10001 주식기본정보
        반환: {cur_price, prev_close, day_high, day_low, change_pct, volume}
        """
        try:
            data = self._post("ka10001", "/api/dostk/stkinfo", {"stk_cd": code})

            def ti(s):
                try:
                    return abs(int((str(s or "0")).lstrip("0+-") or "0"))
                except Exception:
                    return 0

            cur_price  = ti(data.get("cur_prc",   "0"))
            prev_close = ti(data.get("base_pric",  "0"))
            day_high   = ti(data.get("high_pric",  "0"))
            day_low    = ti(data.get("low_pric",   "0"))
            volume     = ti(data.get("trde_qty",   "0"))

            flu_rt = data.get("flu_rt", "0")
            try:
                change_pct = float(flu_rt.lstrip("+-").replace(",", "") or "0")
                if "-" in str(flu_rt):
                    change_pct = -change_pct
            except Exception:
                change_pct = (
                    (cur_price - prev_close) / prev_close * 100
                    if prev_close > 0 else 0.0
                )

            log.debug(f"[Broker] {code} 현재:{cur_price:,} 고:{day_high:,} 저:{day_low:,}")
            return {
                "cur_price":  cur_price,
                "prev_close": prev_close,
                "day_high":   day_high,
                "day_low":    day_low,
                "change_pct": round(change_pct, 2),
                "volume":     volume,
            }
        except Exception as e:
            log.error(f"[Broker] get_stock_info 실패 {code}: {e}")
            return {
                "cur_price": 0, "prev_close": 0,
                "day_high": 0,  "day_low": 0,
                "change_pct": 0.0, "volume": 0,
            }
    def cancel_all_orders(self) -> dict:
        orders    = self.get_open_orders()
        cancelled = sum(
            1 for o in orders
            if self.cancel_order(o["order_no"], o["code"], o["remain"])
        )
        log.info(f"[Broker] 미체결 {cancelled}/{len(orders)}건 취소")
        return {"cancelled": cancelled, "total": len(orders)}
