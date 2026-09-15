"""后台 job：调度、提交纪律、生命周期。**框架无关**——不 import flask，
不碰 request / session（spec §3.1）。

与 CLI 最关键的差异（spec §4.1）：cli.drive() 是一个 while 循环——跑一段、
阻塞在 input() 问人、再跑一段。Web 版把这个循环拆开交给 HTTP：**每一次用户
动作 = 恰好一次 advance()**，跑到下一个暂停点就结束，线程退出。
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from tripplan.agents.limits import Cancelled, LimitExceeded
from tripplan.artifacts import discard as _discard
from tripplan.artifacts import publish as _publish
from tripplan.artifacts import stage_artifacts as _stage_artifacts
from tripplan.orchestrator import advance as _advance
from tripplan.providers.base import ProviderError
from tripplan.repo import FileRepo, TripCorrupt, TripNotFound
from tripplan.state import Done, Rejected, Stage

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobOutcome:
    status: str  # "succeeded" | "rejected" | "failed" | "cancelled"
    kind: str | None = None
    message: str | None = None
    revision: int | None = None

    @classmethod
    def ok(cls, revision: int, kind=None, message=None) -> "JobOutcome":
        return cls("succeeded", kind, message, revision)

    @classmethod
    def rejected(cls, reason) -> "JobOutcome":
        return cls("rejected", "Rejected", getattr(reason, "value", str(reason)))

    @classmethod
    def failed(cls, kind: str, message: str) -> "JobOutcome":
        return cls("failed", kind, message)

    @classmethod
    def cancelled(cls) -> "JobOutcome":
        return cls("cancelled", "Cancelled", "已取消")


def open_trip(trips_root, tid: str) -> FileRepo:
    return FileRepo(Path(trips_root) / tid)


def run_command(
    trips_root,
    tid: str,
    cmd,
    deps,
    job,
    *,
    advance_fn=_advance,
    stage_fn=_stage_artifacts,
    publish_fn=_publish,
    discard_fn=_discard,
    open_fn=open_trip,
) -> JobOutcome:
    try:
        repo = open_fn(trips_root, tid)
        state = repo.load()  # 每次从盘上重读，命令之间不在内存留 state
    except (TripNotFound, TripCorrupt) as e:
        return JobOutcome.failed(type(e).__name__, str(e))

    persisted = state.revision  # CAS 的 expected

    try:
        outcome = advance_fn(state, deps, cmd, job.emit, cancel=job.cancel_token)
    except Cancelled:
        # 不落盘：取消不是一种规划结果。盘上仍是上一个暂停点。
        return JobOutcome.cancelled()
    except (ProviderError, LimitExceeded) as e:
        # 这两个分支接的正是 cli.main() 原来那两个 except 的职责。
        return JobOutcome.failed(type(e).__name__, str(e))

    if isinstance(outcome, Rejected):
        # state 未变、revision 未变 → 不落盘。写盘只会白占一次 CAS 窗口，
        # 让无辜的并发调用被误杀（见 cli.drive() 里同一处的注释）。
        return JobOutcome.rejected(outcome.reason)

    if state.revision == persisted:
        # advance 什么也没改（等待态收到空命令＝重新问一遍）。同上，不落盘。
        return JobOutcome.ok(state.revision)

    staged, artifact_error = None, None
    if isinstance(outcome, Done):
        # 写进 <trip>/.staging/<job_id>/ —— 这一次 job 私有的目录，不是最终
        # 路径，也不是同一 trip 共享的暂存区（spec §4.1）。这一步慢（要拉高德
        # 静态图）也可能失败；失败只记进 JobOutcome，绝不影响下面的 CAS 判定。
        try:
            staged = stage_fn(state, repo.dir, deps.provider, job.job_id)
        except Exception as e:  # noqa: BLE001
            _log.exception("产物暂存失败")
            artifact_error = f"{type(e).__name__}: {e}"

    if job.cancel_token.is_set():
        discard_fn(staged)
        return JobOutcome.cancelled()  # CAS 前最后一道检查：已取消就别写盘

    if not repo.save_if_revision(state, persisted):
        discard_fn(staged)
        # 绝不发布产物：盘上的结局不是我们手上这份。
        return JobOutcome.failed("Conflict", "另一个进程改动了这个行程")

    if staged is not None:
        try:
            publish_fn(staged)  # 逐个 os.replace 原子改名，最后写 artifacts.json
        except Exception as e:  # noqa: BLE001
            _log.exception("产物发布失败")
            artifact_error = f"{type(e).__name__}: {e}"

    if artifact_error is not None:
        # 规划本身成功了（state 已落盘），只是产物没做出来——详情页会显示
        # 「产物待重建」+ 重建按钮，不给死链（spec §4.1）。
        return JobOutcome.ok(state.revision, "ArtifactError", artifact_error)
    return JobOutcome.ok(state.revision)


def rebuild_artifacts(
    trips_root,
    tid: str,
    deps,
    job,
    *,
    stage_fn=_stage_artifacts,
    publish_fn=_publish,
    discard_fn=_discard,
    open_fn=open_trip,
) -> JobOutcome:
    """从 state.json 重跑 stage + publish，**不碰 LLM、不碰 CAS**（spec §6.2）。

    崩溃恢复、旧版本产物残留、手工删文件，三种情况共用这一条出路。
    """
    try:
        repo = open_fn(trips_root, tid)
        state = repo.load()
    except (TripNotFound, TripCorrupt) as e:
        return JobOutcome.failed(type(e).__name__, str(e))

    if state.stage is not Stage.DONE:
        return JobOutcome.failed("NotDone", "行程还没定稿，没有可重建的成稿产物")

    staged = None
    try:
        staged = stage_fn(state, repo.dir, deps.provider, job.job_id)
        if job.cancel_token.is_set():
            discard_fn(staged)
            return JobOutcome.cancelled()
        publish_fn(staged)
    except Exception as e:  # noqa: BLE001
        _log.exception("产物重建失败")
        discard_fn(staged)
        return JobOutcome.failed(type(e).__name__, str(e))

    return JobOutcome.ok(state.revision)


#: **唯一的 active 定义。不许有任何一处写成只判断 "running"**（spec §4.2）。
#: 取消不是立刻生效的，也没有时延上界：一个 cancelling 的 job，它的线程可能
#: 还卡在一次已经发出去的 LLM 请求上，还在烧钱，还握着那个 trip 的 state。
ACTIVE = frozenset({"running", "cancelling"})

_TERMINAL_EVENT = {
    "succeeded": "job_succeeded",
    "rejected": "job_rejected",
    "failed": "job_failed",
    "cancelled": "job_cancelled",
}


class TripBusy(Exception):
    """该 trip 已有 active job。路由层转成 409。"""


class ServerBusy(Exception):
    """全局 active job 数已满。路由层转成 503——不排队（spec §4.1.1）。"""


class TripJob:
    """不是「跑完就没人管的一个线程」：详情页和轮询都要能回答
    「刚才那次命令怎么样了」，所以它有一个明确的状态机（spec §4.1.1）。

        running ──┬─→ succeeded
                  ├─→ rejected
                  ├─→ failed
                  └─→ cancelling ─→ cancelled
    """

    def __init__(self, tid: str, log, clock=time.time) -> None:
        #: uuid4 hex，**永不复用**。它同时是暂存目录名（spec §4.1）和前端的
        #: 身份判据：status_version 是 per-job 的，新 job 从 1 重新开始，于是
        #: 「老 job 的 running(1)」与「新 job 的 running(1)」在前端看来一模
        #: 一样。对外暴露的身份必须是 (job_id, status_version) 这个二元组。
        self.job_id = uuid.uuid4().hex
        self.tid = tid
        self.cancel_token = threading.Event()
        self.status = "running"
        self.status_version = 1
        self.kind: str | None = None
        self.message: str | None = None
        self.thread: threading.Thread | None = None
        self.finished_at: float | None = None
        self._log = log
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    # ---------- 事件 ----------

    def emit(self, event) -> None:
        """**不抛异常的边界**（spec §5.1.1）。事件日志坏了就少几行进度，
        它没有资格决定一次规划算不算数。

        顺带做 §5.1 的信封归一：现有 emit 收的是变长 tuple，这里摊成
        type + {"args": [...]}，**不修改 orchestrator 的 emit 契约**。
        """
        try:
            parts = tuple(event)
            self._log.append(str(parts[0]), {"args": list(parts[1:])})
        except Exception:  # noqa: BLE001
            _log.exception("事件记录失败，已忽略")

    def emit_terminal(self, outcome: JobOutcome) -> None:
        """终态写 durable event，并 flush 一次。

        带 job_id：重启后回读 events.jsonl 也能把终态对到具体某一次命令上，
        而不是只知道「有过一次失败」（spec §4.1.1）。
        """
        try:
            self._log.append(
                _TERMINAL_EVENT.get(outcome.status, "job_failed"),
                {
                    "job_id": self.job_id,
                    "kind": outcome.kind,
                    "message": outcome.message,
                },
            )
            self._log.flush()
        except Exception:  # noqa: BLE001
            _log.exception("终态事件记录失败，已忽略")

    # ---------- 状态迁移 ----------

    def request_cancel(self) -> bool:
        """幂等：已经是 cancelling / 终态时是 no-op，**不再 +1
        status_version**（否则每点一下都让所有标签页白刷一次，spec §6.2）。"""
        with self._lock:
            if self.status != "running":
                return False
            self.cancel_token.set()
            self.status = "cancelling"
            self.status_version += 1
            return True

    def finish(self, outcome: JobOutcome) -> None:
        with self._lock:
            self.status = outcome.status
            self.kind = outcome.kind
            self.message = outcome.message
            self.status_version += 1
            self.finished_at = self._clock()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.job_id,
                "status": self.status,
                "status_version": self.status_version,
                "kind": self.kind,
                "message": self.message,
            }


class JobRegistry:
    """**进程内单例**（spec §6.4）。多 worker 会让轮询请求被路由到没有该 job
    的进程，进度页随机失灵、取消按钮随机失效——这正是选 waitress（单进程
    多线程）而不是 gunicorn 的实质理由。"""

    def __init__(self, max_jobs: int = 3, retain_s: float = 1800.0, clock=time.time):
        self._max_jobs = max_jobs
        self._retain_s = retain_s
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: dict[str, TripJob] = {}

    def get(self, tid: str) -> TripJob | None:
        with self._lock:
            return self._jobs.get(tid)

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.active)

    def start(self, tid: str, log, target) -> TripJob:
        """target(job) -> JobOutcome。两个约束在同一把锁里判，避免竞态。"""
        with self._lock:
            self._reap_locked()
            existing = self._jobs.get(tid)
            if existing is not None and existing.active:
                raise TripBusy(tid)
            if sum(1 for j in self._jobs.values() if j.active) >= self._max_jobs:
                # 不排队：排队要额外引入队列超时、取消排队中的 job，以及
                # 「按钮点了但什么都没发生」的解释成本（spec §4.1.1）。
                raise ServerBusy()

            job = TripJob(tid, log, clock=self._clock)
            self._jobs[tid] = job
            job.thread = threading.Thread(
                target=self._run,
                args=(job, target),
                name=f"trip-job-{tid}",
                daemon=True,
            )
            job.thread.start()
            return job

    @staticmethod
    def _run(job: TripJob, target) -> None:
        """线程体必须 try/except/finally 收尾（spec §4.1.1）。

        except BaseException 兜住任何未预料的异常并转成 failed；finally 里
        无条件置终态 + flush EventLog。少了这层，一个没想到的异常会让 job
        永远停在 running，详情页的按钮就永久置灰了。
        """
        outcome = JobOutcome.failed("UnknownError", "job 线程异常退出")
        try:
            outcome = target(job)
        except BaseException as e:  # noqa: BLE001 — 故意兜底，见 docstring
            _log.exception("job 线程未预料异常")
            outcome = JobOutcome.failed(type(e).__name__, str(e) or type(e).__name__)
        finally:
            # 顺序刻意：前端看到终态就停止轮询，终态事件必须先在流里。
            job.emit_terminal(outcome)
            job.finish(outcome)

    def _reap_locked(self) -> None:
        """终态 job 留在 registry 里供轮询读取，直到被新 job 替换或超过保留
        窗口。**active 的 job 永远不会被清理**——cancelling 也算 active。
        丢了也不损失信息，终态已经在 events.jsonl 里（spec §4.1.1）。"""
        now = self._clock()
        for tid, job in list(self._jobs.items()):
            if job.active or job.finished_at is None:
                continue
            if now - job.finished_at > self._retain_s:
                del self._jobs[tid]
