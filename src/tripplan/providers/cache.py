"""按 key 去重的磁盘缓存。LLM 工具与 resolver 共用同一份。"""

import hashlib
import json
import os
import tempfile
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
        except (ValueError, KeyError, OSError):
            # ValueError 覆盖 JSONDecodeError 与 UnicodeDecodeError 两者
            # （都是它的子类）——写入中途崩溃可能把多字节字符切在半当中，
            # 那种坏法解码时抛的是 UnicodeDecodeError，不是 JSONDecodeError，
            # 漏掉它就违反了「缓存坏了就当没命中」的承诺。
            return None  # 缓存坏了就当没命中，不让它污染调用方

    def put(self, key: str, value) -> None:
        payload = {"key": key, "value": value}
        text = json.dumps(payload, ensure_ascii=False)
        # 原子写：先写临时文件再 os.replace，同 repo.py._atomic_write 的做法——
        # 避免进程在写一半时崩溃，在缓存目录里留下半个 JSON 文件。
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".cache-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path(key))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
