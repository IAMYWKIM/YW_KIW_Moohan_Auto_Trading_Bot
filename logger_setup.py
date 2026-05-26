# ==============================================================
# [logger_setup.py] 로거 초기화 모듈 v3.0
# - 기존 etf_bot 로거 유지
# - setup_logger() 함수 추가 (main.py v3 호환)
# ==============================================================
import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR  = "logs"
LOG_FILE = os.path.join(LOG_DIR, "etf_bot.log")
FMT      = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
DATE_FMT = "%Y-%m-%d %H:%M:%S"


def setup_logger():
    """
    루트 로거 초기화.
    - 파일: logs/etf_bot.log  (최대 5MB × 3개 롤오버)
    - 콘솔: stdout
    중복 등록 방지: 핸들러가 이미 있으면 건너뜀.
    """
    os.makedirs(LOG_DIR, exist_ok=True)

    root = logging.getLogger()
    if root.handlers:          # 이미 초기화된 경우 건너뜀
        return

    formatter = logging.Formatter(FMT, datefmt=DATE_FMT)

    # 파일 핸들러 (롤오버)
    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(formatter)

    # 콘솔 핸들러
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)

    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(sh)

    logging.getLogger("etf_bot").info(
        f"[Logger] 로깅 초기화 완료 — 로그 디렉토리: {LOG_DIR}/"
    )
