"""后台 job：调度、提交纪律、生命周期。**框架无关**——不 import flask，
不碰 request / session（spec §3.1）。

与 CLI 最关键的差异（spec §4.1）：cli.drive() 是一个 while 循环——跑一段、
阻塞在 input() 问人、再跑一段。Web 版把这个循环拆开交给 HTTP：**每一次用户
动作 = 恰好一次 advance()**，跑到下一个暂停点就结束，线程退出。
"""

import logging
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
