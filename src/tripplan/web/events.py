"""事件日志。两个来源，一份接口（spec §5.5）。

| 来源 | 内容 | 服务谁 |
|---|---|---|
| events.jsonl（磁盘） | 全部 durable=True 历史 | snapshot() 的主体 |
| live ring（内存） | 最近 N 条，含 durable=False | since() 的增量轮询；并给 snapshot() 补尾巴 |

ring 有容量上限，装不下全部历史；而把全部历史留在内存里，下期几十上百条/秒
的 token 事件立刻把它撑爆（spec §5.3 的整个前提）。所以两者都要，且必须分开。

本模块不 import flask：`?since=N` 的轮询与下期的 SSE（Last-Event-ID 就是 seq）
是同一份数据的两种取法（spec §5.3 第 3 点）。
"""

import collections
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Event:
    seq: int  # 单调递增，从 1 开始
    ts: float  # epoch 秒
    type: str  # "generating" / "revision" / 下期的 "token"
    payload: dict  # 结构化，JSON 可序列化
    #: 同 id 的事件在前端拼进同一个块。v1 不产生这类事件，但渲染函数现在就认它。
    stream_id: str | None = None
    #: False 只进内存，不落盘。下期 token 事件靠它不撑爆 events.jsonl。
    durable: bool = True

    def to_json(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "payload": self.payload,
            "stream_id": self.stream_id,
        }


@dataclass(frozen=True)
class Snapshot:
    events: list[Event]
    #: **此刻的 high-water mark**（已分配出去的最大 seq），不是「磁盘上的最大
    #: seq」。收敛性全靠这一点：紧接着的 since(cursor) 在构造上不可能再 reset。
    cursor: int
    first_seq: int
    stream_epoch: str


@dataclass(frozen=True)
class SinceResult:
    events: list[Event]
    first_seq: int
    last_seq: int
    stream_epoch: str
    reset_required: bool
    resume_seq: int | None


class EventLog:
    def __init__(
        self,
        path,
        ring_size: int = 2000,
        stream_epoch: str | None = None,
        clock=time.time,
        flush_every: int = 20,
    ) -> None:
        self._path = Path(path)
        self._clock = clock
        self._flush_every = flush_every
        self._lock = threading.Lock()
        self._ring: collections.deque[Event] = collections.deque(maxlen=ring_size)
        self._pending: list[Event] = []
        self.stream_epoch = stream_epoch or secrets.token_hex(4)
        self._history = self._read_history()
        self._seq = self._history[-1].seq if self._history else 0

    # ---------- 写 ----------

    def append(self, type: str, payload: dict, stream_id=None, durable=True) -> Event:
        """可能抛（序列化失败 / 磁盘满 / 文件只读）。调用方 TripJob.emit 负责
        兜住——那里才是「不抛异常的边界」（spec §5.1.1）。"""
        with self._lock:
            self._seq += 1
            event = Event(self._seq, self._clock(), type, payload, stream_id, durable)
            self._ring.append(event)
            if durable:
                self._history.append(event)
                self._pending.append(event)
                if len(self._pending) >= self._flush_every:
                    self._flush_locked()
            return event

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    # ---------- 读 ----------

    def snapshot(self) -> Snapshot:
        """详情页服务端渲染时的初始快照。三件事必须**在同一个锁内**完成：
        读 durable 历史、取 ring 全部内容、按 seq 归并去重。否则「读完磁盘」
        与「读 ring」之间新写入的事件会掉进缝里，快照和游标对不上——v1 的
        批量 flush 延迟就足以制造这条缝（spec §5.5）。"""
        with self._lock:
            merged: dict[int, Event] = {e.seq: e for e in self._history}
            merged.update({e.seq: e for e in self._ring})
            return Snapshot(
                events=[merged[k] for k in sorted(merged)],
                cursor=self._seq,
                first_seq=self._first_seq_locked(),
                stream_epoch=self.stream_epoch,
            )

    def since(self, n: int, epoch: str | None = None) -> SinceResult:
        with self._lock:
            first = self._first_seq_locked()
            stale_epoch = epoch is not None and epoch != self.stream_epoch
            # off-by-one 是刻意的：ring 首元素 seq = first_seq，所以游标
            # first_seq - 1 的客户端要的正好是 ring 的全部内容，服务得了。
            # 写成 `n < first` 会让「空 ring + since=0」也判成失效，而 reload
            # 之后拿到的还是 0 —— 一个每秒 reload 一次的死循环。
            if stale_epoch or n < first - 1:
                return SinceResult(
                    [], first, self._seq, self.stream_epoch, True, self._seq
                )
            return SinceResult(
                [e for e in self._ring if e.seq > n],
                first,
                self._seq,
                self.stream_epoch,
                False,
                None,
            )

    # ---------- 内部 ----------

    def _first_seq_locked(self) -> int:
        """ring 能服务的最早 seq。ring 空时是「下一个事件将拿到的号」。"""
        return self._ring[0].seq if self._ring else self._seq + 1

    def _flush_locked(self) -> None:
        if not self._pending:
            return
        lines = "".join(
            json.dumps(e.to_json(), ensure_ascii=False, default=str) + "\n"
            for e in self._pending
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(lines)
        # 不每条 fsync：这份日志是给人看的进度历史，不是权威状态（spec §5.4）。
        self._pending.clear()

    def _read_history(self) -> list[Event]:
        if not self._path.exists():
            return []
        out: list[Event] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                out.append(
                    Event(
                        seq=int(raw["seq"]),
                        ts=float(raw["ts"]),
                        type=str(raw["type"]),
                        payload=raw.get("payload") or {},
                        stream_id=raw.get("stream_id"),
                        durable=True,
                    )
                )
            except (ValueError, KeyError, TypeError):
                continue  # 崩在半行上留下的残片：跳过，不是致命错误
        out.sort(key=lambda e: e.seq)
        return out


class EventLogStore:
    """进程内的 tid → EventLog 映射。

    stream_epoch 是**进程级**的：它回答的是「这还是同一条流吗」，重启后
    所有 trip 的流都换了（spec §5.5）。
    """

    def __init__(self, trips_root, ring_size: int = 2000) -> None:
        self._root = Path(trips_root)
        self._ring_size = ring_size
        self._lock = threading.Lock()
        self._logs: dict[str, EventLog] = {}
        self.stream_epoch = secrets.token_hex(4)

    def get(self, tid: str) -> EventLog:
        with self._lock:
            log = self._logs.get(tid)
            if log is None:
                log = EventLog(
                    self._root / tid / "events.jsonl",
                    ring_size=self._ring_size,
                    stream_epoch=self.stream_epoch,
                )
                self._logs[tid] = log
            return log
