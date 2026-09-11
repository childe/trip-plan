"""按 key 去重的磁盘缓存。LLM 工具与 resolver 共用同一份。"""

import hashlib
import json
import time
from pathlib import Path


class DiskCache:
    def __init__(self, root: Path, ttl_days: int = 7) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_days * 86400

    def _path(self, key: str) -> Path:
        # 哈希做文件名：既避免路径穿越，也不受 key 长度与字符集限制
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def get(self, key: str):
        path = self._path(key)
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > self.ttl_seconds:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))["value"]
        except (json.JSONDecodeError, KeyError, OSError):
            return None  # 缓存坏了就当没命中，不让它污染调用方

    def put(self, key: str, value) -> None:
        payload = {"key": key, "value": value}
        self._path(key).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
