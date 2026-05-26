# ==============================================================
# [database.py] SQLite 영속성 관리 v3.0
# - trades 테이블 (당일 체결 내역)
# - positions 테이블 (회차/평단가 상태)
# - 재시작 후에도 상태 복원 보장
# ==============================================================
import sqlite3
import logging
import datetime
import os
from threading import Lock
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

DB_PATH = os.path.join("data", "trading_state.db")


class Database:
    """SQLite 기반 매매 상태 영속성 관리."""

    def __init__(self, path: str = DB_PATH):
        self._path = path
        self._lock = Lock()
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with self._lock, self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS positions (
                    code        TEXT PRIMARY KEY,
                    name        TEXT,
                    mode        TEXT DEFAULT 'INFINITE',
                    round_no    INTEGER DEFAULT 0,
                    avg_price   REAL    DEFAULT 0.0,
                    total_qty   INTEGER DEFAULT 0,
                    target_value REAL   DEFAULT 0.0,
                    updated_at  TEXT
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    code        TEXT,
                    name        TEXT,
                    side        TEXT,
                    qty         INTEGER,
                    price       INTEGER,
                    amount      INTEGER,
                    profit      INTEGER DEFAULT 0,
                    profit_pct  REAL    DEFAULT 0.0,
                    order_no    TEXT,
                    trade_date  TEXT,
                    trade_time  TEXT
                );

                CREATE TABLE IF NOT EXISTS vrev_state (
                    code        TEXT PRIMARY KEY,
                    target_value REAL DEFAULT 0.0,
                    last_rebal_date TEXT,
                    updated_at  TEXT
                );

                CREATE TABLE IF NOT EXISTS cycle_log (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    code          TEXT,
                    name          TEXT,
                    round_no      INTEGER DEFAULT 1,
                    start_date    TEXT,
                    end_date      TEXT,
                    principal     INTEGER DEFAULT 0,
                    final_amount  INTEGER DEFAULT 0,
                    profit        INTEGER DEFAULT 0,
                    profit_pct    REAL    DEFAULT 0.0,
                    final_t_val   REAL    DEFAULT 0.0,
                    exit_type     TEXT    DEFAULT '목표가달성',
                    created_at    TEXT
                );
            """)
        log.info(f"✅ [DB] 초기화 완료 ({self._path})")

    # ----------------------------------------------------------
    # positions — 회차/평단가 관리
    # ----------------------------------------------------------
    def get_position(self, code: str) -> dict:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM positions WHERE code=?", (code,)
            ).fetchone()
            return dict(row) if row else {}

    def get_all_positions(self) -> list:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT * FROM positions").fetchall()
            return [dict(r) for r in rows]

    def upsert_position(self, code: str, name: str = "", mode: str = "INFINITE",
                        round_no: int = None, avg_price: float = None,
                        total_qty: int = None, target_value: float = None):
        now = datetime.datetime.now(KST).isoformat()
        with self._lock, self._conn() as conn:
            existing = conn.execute(
                "SELECT * FROM positions WHERE code=?", (code,)
            ).fetchone()
            if existing:
                updates = {"updated_at": now}
                if round_no is not None:    updates["round_no"]    = round_no
                if avg_price is not None:   updates["avg_price"]   = avg_price
                if total_qty is not None:   updates["total_qty"]   = total_qty
                if target_value is not None: updates["target_value"] = target_value
                set_clause = ", ".join(f"{k}=?" for k in updates)
                conn.execute(
                    f"UPDATE positions SET {set_clause} WHERE code=?",
                    (*updates.values(), code),
                )
            else:
                conn.execute(
                    """INSERT INTO positions
                       (code, name, mode, round_no, avg_price, total_qty, target_value, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (code, name, mode,
                     round_no or 0,
                     avg_price or 0.0,
                     total_qty or 0,
                     target_value or 0.0,
                     now),
                )
        log.debug(f"[DB] position upsert: {code}")

    def has_holdings(self, code: str) -> bool:
        """보유 잔량 있는지 확인 — 설정 변경 안전장치."""
        pos = self.get_position(code)
        return pos.get("total_qty", 0) > 0

    # ----------------------------------------------------------
    # trades — 체결 내역 기록
    # ----------------------------------------------------------
    def record_trade(self, code: str, name: str, side: str,
                     qty: int, price: int, order_no: str = "",
                     profit: int = 0, profit_pct: float = 0.0):
        now = datetime.datetime.now(KST)
        amount = qty * price
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO trades
                   (code, name, side, qty, price, amount, profit, profit_pct,
                    order_no, trade_date, trade_time)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (code, name, side, qty, price, amount, profit, profit_pct,
                 order_no,
                 now.strftime("%Y-%m-%d"),
                 now.strftime("%H:%M:%S")),
            )
        log.info(f"[DB] 체결 기록: {side} {code} {qty}주 @ {price:,}원")

    def get_trades_by_date(self, date_str: str) -> list:
        """당일(YYYY-MM-DD) 체결 내역 조회."""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE trade_date=? ORDER BY trade_time",
                (date_str,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_trades_by_code(self, code: str, limit: int = 50) -> list:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE code=? ORDER BY id DESC LIMIT ?",
                (code, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def cleanup_old_trades(self, days: int = 30):
        """지정 일 이상 된 체결 내역 삭제."""
        cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        with self._lock, self._conn() as conn:
            result = conn.execute(
                "DELETE FROM trades WHERE trade_date < ?", (cutoff,)
            )
            if result.rowcount > 0:
                log.info(f"[DB] {result.rowcount}건 체결 내역 정리 ({cutoff} 이전)")

    # ----------------------------------------------------------
    # V-REV 상태 관리
    # ----------------------------------------------------------
    def get_vrev_state(self, code: str) -> dict:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM vrev_state WHERE code=?", (code,)
            ).fetchone()
            return dict(row) if row else {}

    def upsert_vrev_state(self, code: str, target_value: float,
                           last_rebal_date: str = ""):
        now = datetime.datetime.now(KST).isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO vrev_state (code, target_value, last_rebal_date, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(code) DO UPDATE SET
                       target_value=excluded.target_value,
                       last_rebal_date=excluded.last_rebal_date,
                       updated_at=excluded.updated_at""",
                (code, target_value, last_rebal_date, now),
            )
        log.debug(f"[DB] vrev_state upsert: {code} target={target_value:,}")
    # ----------------------------------------------------------
    # cycle_log — 졸업(사이클 완료) 기록
    # ----------------------------------------------------------
    def record_cycle_graduation(
        self,
        code:         str,
        name:         str,
        round_no:     int,
        principal:    int,
        final_amount: int,
        final_t_val:  float,
        exit_type:    str   = "목표가달성",
        start_date:   str   = "",
    ):
        """사이클 완료(졸업) 기록."""
        now    = datetime.datetime.now(KST)
        profit = final_amount - principal
        pct    = profit / principal * 100 if principal > 0 else 0.0
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO cycle_log
                   (code, name, round_no, start_date, end_date,
                    principal, final_amount, profit, profit_pct,
                    final_t_val, exit_type, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (code, name, round_no,
                 start_date or "",
                 now.strftime("%Y-%m-%d"),
                 principal, final_amount,
                 profit, round(pct, 2),
                 round(final_t_val, 4),
                 exit_type,
                 now.isoformat()),
            )
        log.info(
            f"[DB] 졸업 기록: {name}({code}) {round_no}회차 "
            f"수익 {profit:+,}원 ({pct:+.2f}%)"
        )

    def get_cycle_log(self, code: str = None, limit: int = 20) -> list:
        """졸업 기록 조회. code 없으면 전체."""
        with self._lock, self._conn() as conn:
            if code:
                rows = conn.execute(
                    "SELECT * FROM cycle_log WHERE code=? "
                    "ORDER BY id DESC LIMIT ?",
                    (code, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM cycle_log ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_cycle_stats(self, code: str = None) -> dict:
        """졸업 통계 (총 회차, 총 수익, 승률 등)."""
        with self._lock, self._conn() as conn:
            where = "WHERE code=?" if code else ""
            params = (code,) if code else ()
            row = conn.execute(
                f"""SELECT
                    COUNT(*)            AS total,
                    SUM(profit)         AS total_profit,
                    AVG(profit_pct)     AS avg_pct,
                    MAX(profit_pct)     AS best_pct,
                    MIN(profit_pct)     AS worst_pct,
                    SUM(CASE WHEN profit >= 0 THEN 1 ELSE 0 END) AS wins
                FROM cycle_log {where}""",
                params,
            ).fetchone()
            if not row or row["total"] == 0:
                return {}
            return {
                "total":        row["total"],
                "total_profit": int(row["total_profit"] or 0),
                "avg_pct":      round(row["avg_pct"] or 0, 2),
                "best_pct":     round(row["best_pct"] or 0, 2),
                "worst_pct":    round(row["worst_pct"] or 0, 2),
                "wins":         row["wins"],
                "win_rate":     round(row["wins"] / row["total"] * 100, 1),
            }
