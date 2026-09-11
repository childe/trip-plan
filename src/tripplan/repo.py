"""状态仓储。一个实例对应一个 trip 目录。

CAS 语义在接口里：save_if_revision 仅当盘上 revision 仍等于 expected 时才写。
FileRepo 用 flock 把「检查」和「写入」合成一个临界区，未来的 DbRepo 用
UPDATE ... WHERE revision = ? 的影响行数判断，语义完全一致。
"""

import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Protocol

from tripplan.state import TripState
from tripplan.wire import dumps, loads


class TripExists(Exception):
    pass


class TripNotFound(Exception):
    pass


class TripCorrupt(Exception):
    """state.json 存在但无法解析——文件已损坏，不是合法的 wire format。"""

    pass


class StateRepo(Protocol):
    def create(self, state: TripState) -> None: ...
    def load(self) -> TripState: ...
    def save_if_revision(self, state: TripState, expected: int) -> bool: ...


class FileRepo:
    def __init__(self, trip_dir: Path) -> None:
        self.dir = Path(trip_dir)

    @property
    def _state_path(self) -> Path:
        return self.dir / "state.json"

    @property
    def _lock_path(self) -> Path:
        return self.dir / ".lock"

    def create(self, state: TripState) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock_path.touch(exist_ok=True)
        payload = dumps(state)  # 失败就不会留下任何文件
        # 'x' 模式：已存在就抛，天然防住 run_id 撞车
        try:
            with open(self._state_path, "x", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
        except FileExistsError as e:
            raise TripExists(f"{self._state_path} 已存在，不覆盖") from e

    def load(self) -> TripState:
        if not self._state_path.exists():
            raise TripNotFound(str(self._state_path))
        return self._decode(self._state_path.read_text(encoding="utf-8"))

    def save_if_revision(self, state: TripState, expected: int) -> bool:
        if not self._state_path.exists():
            raise TripNotFound(str(self._state_path))
        with open(self._lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # 临界区开始
            try:
                on_disk = self._decode(self._state_path.read_text(encoding="utf-8"))
                if on_disk.revision != expected:
                    return False  # 有人抢先，不写
                self._atomic_write(dumps(state))
                return True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _decode(self, text: str) -> TripState:
        """把「解析失败」变成一个说得清楚的错误，而不是一截 JSON 栈回溯。"""
        try:
            return loads(text)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            raise TripCorrupt(
                f"{self._state_path} 无法解析：文件已损坏，"
                "建议删除该 trip 目录后重新运行"
            ) from e

    def _atomic_write(self, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._state_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
