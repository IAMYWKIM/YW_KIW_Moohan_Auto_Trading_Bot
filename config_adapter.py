# ==============================================================
# [config_adapter.py] 설정 어댑터 v3.0
# KiwoomBroker.cfg 인터페이스 — telegram_bot / scheduler 에서 사용
# config.json 과 .env 를 통합하여 단일 get/set 인터페이스 제공
# ==============================================================
import json
import os
import logging
from threading import Lock

log = logging.getLogger(__name__)

CONFIG_PATH = "config.json"


class ConfigAdapter:
    """config.json 읽기/쓰기 + 런타임 동적 설정 관리."""

    def __init__(self, path: str = CONFIG_PATH):
        self._path = path
        self._lock = Lock()
        self._data: dict = {}
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                log.info(f"[Config] {self._path} 로드 완료")
            except Exception as e:
                log.error(f"[Config] 로드 실패: {e}")
                self._data = {}
        else:
            log.warning(f"[Config] {self._path} 없음 — 기본값 사용")
            self._data = {}

    def _save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"[Config] 저장 실패: {e}")

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value):
        with self._lock:
            self._data[key] = value
            self._save()
        log.info(f"[Config] {key} = {value} 저장")

    def get_symbols(self) -> list:
        return self.get("SYMBOLS", [])

    def get_active_symbols(self) -> list:
        return [s for s in self.get_symbols() if s.get("active", True)]

    def reload(self):
        """파일 변경 시 런타임 리로드."""
        with self._lock:
            self._load()
        log.info("[Config] 설정 리로드 완료")
