"""job 生命周期、per-trip 互斥、全局上限（spec §4.1.1 / §4.2 / §9 回归 18、21、22、23）。"""

import json
import threading

import pytest

from tripplan.web.events import EventLog
from tripplan.web.jobs import JobOutcome, JobRegistry, ServerBusy, TripBusy, TripJob


def _log(tmp_path, tid="kyoto"):
    return EventLog(tmp_path / tid / "events.jsonl")


def _blocking_target(gate):
    def target(job):
        gate.wait(5)
        return JobOutcome.ok(1)

    return target


def test_a_finished_job_reports_its_terminal_status(tmp_path):
    reg = JobRegistry()
    job = reg.start("kyoto", _log(tmp_path), lambda j: JobOutcome.ok(7))
    job.thread.join(5)
    assert job.status == "succeeded"
    assert job.active is False
    assert reg.get("kyoto") is job


def test_status_version_starts_at_one_and_increments_on_every_change(tmp_path):
    job = TripJob("kyoto", _log(tmp_path))
    assert (job.status, job.status_version) == ("running", 1)
    job.request_cancel()
    assert (job.status, job.status_version) == ("cancelling", 2)
    job.finish(JobOutcome.cancelled())
    assert (job.status, job.status_version) == ("cancelled", 3)


def test_repeated_cancel_is_a_no_op_and_does_not_bump_the_version(tmp_path):
    """否则每点一下取消都让所有标签页白刷一次（spec §6.2）。"""
    job = TripJob("kyoto", _log(tmp_path))
    assert job.request_cancel() is True
    assert job.request_cancel() is False
    assert job.status_version == 2
    assert job.cancel_token.is_set()


def test_cancelling_counts_as_active(tmp_path):
    """spec §4.2：cancelling 是**正在退出**，不是**已经退出**。"""
    job = TripJob("kyoto", _log(tmp_path))
    job.request_cancel()
    assert job.active is True


def test_the_same_trip_cannot_start_two_jobs(tmp_path):
    """§9 回归 1 的 registry 层。互斥是为了省钱：输掉 CAS 的那个线程
    已经把 LLM 的钱烧完了才发现自己白干（spec §4.2）。"""
    reg = JobRegistry()
    gate = threading.Event()
    reg.start("kyoto", _log(tmp_path), _blocking_target(gate))
    with pytest.raises(TripBusy):
        reg.start("kyoto", _log(tmp_path), lambda j: JobOutcome.ok(1))
    gate.set()


def test_a_cancelling_job_still_blocks_the_same_trip(tmp_path):
    """§9 回归 23 的一部分：那个线程还没退出，放第二个进来就是两个线程
    同时对一份 state 跑 advance。"""
    reg = JobRegistry()
    gate = threading.Event()
    job = reg.start("kyoto", _log(tmp_path), _blocking_target(gate))
    job.request_cancel()
    with pytest.raises(TripBusy):
        reg.start("kyoto", _log(tmp_path), lambda j: JobOutcome.ok(1))
    gate.set()
    job.thread.join(5)
    reg.start("kyoto", _log(tmp_path), lambda j: JobOutcome.ok(1))  # 退出后恢复正常


def test_a_cancelling_job_still_occupies_a_global_slot(tmp_path):
    """§9 回归 23 的另一部分：名额只在线程真正退出时归还。提前释放等于
    允许两个线程同时跑 advance，既烧双份钱又让互斥形同虚设（spec §4.2）。"""
    reg = JobRegistry(max_jobs=1)
    gate = threading.Event()
    job = reg.start("kyoto", _log(tmp_path), _blocking_target(gate))
    job.request_cancel()
    with pytest.raises(ServerBusy):
        reg.start("osaka", _log(tmp_path, "osaka"), lambda j: JobOutcome.ok(1))
    gate.set()
    job.thread.join(5)
    reg.start("osaka", _log(tmp_path, "osaka"), lambda j: JobOutcome.ok(1))


def test_the_global_cap_rejects_a_second_trip(tmp_path):
    """§9 回归 18 的 registry 层：满载即拒，不排队（spec §4.2）。"""
    reg = JobRegistry(max_jobs=1)
    gate = threading.Event()
    reg.start("kyoto", _log(tmp_path), _blocking_target(gate))
    with pytest.raises(ServerBusy):
        reg.start("osaka", _log(tmp_path, "osaka"), lambda j: JobOutcome.ok(1))
    gate.set()


def test_an_unexpected_exception_never_leaves_a_job_running(tmp_path):
    """§9 回归 21：少了 try/except/finally 这层，一个没想到的异常会让 job
    永远停在 running，详情页的按钮就永久置灰了，用户除了重启服务没有任何
    出路（spec §4.1.1）。"""
    reg = JobRegistry()
    job = reg.start(
        "kyoto", _log(tmp_path), lambda j: (_ for _ in ()).throw(KeyError("boom"))
    )
    job.thread.join(5)
    assert job.status == "failed"
    assert job.kind == "KeyError"


def test_a_terminal_durable_event_lands_on_disk_with_the_job_id(tmp_path):
    """§9 回归 21 后半条 + spec §4.1.1：JobRegistry 是进程内内存对象，
    服务器一重启就空了；有这条落盘记录，重启后进详情页仍能解释
    「上次那步发生了什么」，而不是一片空白。"""
    reg = JobRegistry()
    log = _log(tmp_path)
    job = reg.start("kyoto", log, lambda j: (_ for _ in ()).throw(KeyError("boom")))
    job.thread.join(5)

    lines = (
        (tmp_path / "kyoto" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    )
    terminal = [
        json.loads(x) for x in lines if json.loads(x)["type"].startswith("job_")
    ]
    assert terminal[-1]["type"] == "job_failed"
    assert terminal[-1]["payload"]["job_id"] == job.job_id


def test_the_terminal_event_is_written_before_the_status_flips(tmp_path):
    """顺序是刻意的：前端看到终态就停止轮询，终态事件必须先在流里。"""
    reg = JobRegistry()
    log = _log(tmp_path)
    seen = []
    original = log.append

    def spy(type, payload, stream_id=None, durable=True):
        if type.startswith("job_"):
            seen.append(reg.get("kyoto").status)
        return original(type, payload, stream_id, durable)

    log.append = spy
    job = reg.start("kyoto", log, lambda j: JobOutcome.ok(1))
    job.thread.join(5)
    assert seen == ["running"]  # 写终态事件的那一刻，status 还没翻


def test_emit_never_raises_even_when_the_log_is_broken(tmp_path):
    """spec §5.1.1：job.emit() 自己是不抛异常的边界。"""
    log = _log(tmp_path)

    def boom(*a, **kw):
        raise OSError("磁盘满")

    log.append = boom
    job = TripJob("kyoto", log)
    job.emit(("generating", "A"))  # 不抛
    job.emit(("requirements_patched", {"destination": "大阪"}))


def test_emit_normalises_the_tuple_envelope(tmp_path):
    """现有 emit 收的是变长 tuple；Web 层装一个 adapter 归一化，
    **不修改 orchestrator 的 emit 契约**（spec §5.1）。"""
    log = _log(tmp_path)
    job = TripJob("kyoto", log)
    job.emit(("revision", "foodie", 2))
    ev = log.since(0).events[-1]
    assert ev.type == "revision"
    assert ev.payload == {"args": ["foodie", 2]}


def test_a_new_job_replaces_the_previous_terminal_one(tmp_path):
    """§9 回归 22 的 registry 层：job_id 永不复用，status_version 从 1 重来。"""
    reg = JobRegistry()
    first = reg.start("kyoto", _log(tmp_path), lambda j: JobOutcome.failed("X", "y"))
    first.thread.join(5)
    second = reg.start("kyoto", _log(tmp_path), lambda j: JobOutcome.ok(1))
    second.thread.join(5)

    assert first.job_id != second.job_id
    assert reg.get("kyoto") is second


def test_terminal_jobs_are_reaped_after_the_retention_window(tmp_path):
    clock = {"t": 1000.0}
    reg = JobRegistry(retain_s=1800, clock=lambda: clock["t"])
    job = reg.start("kyoto", _log(tmp_path), lambda j: JobOutcome.ok(1))
    job.thread.join(5)

    clock["t"] += 1801
    reg.start("osaka", _log(tmp_path, "osaka"), lambda j: JobOutcome.ok(1)).thread.join(
        5
    )
    assert reg.get("kyoto") is None


def test_active_jobs_are_never_reaped(tmp_path):
    """spec §4.1.1：cancelling 也算 active，它的线程还活着。"""
    clock = {"t": 1000.0}
    reg = JobRegistry(retain_s=1, clock=lambda: clock["t"])
    gate = threading.Event()
    job = reg.start("kyoto", _log(tmp_path), _blocking_target(gate))
    job.request_cancel()
    clock["t"] += 9999
    reg.start("osaka", _log(tmp_path, "osaka"), lambda j: JobOutcome.ok(1))
    assert reg.get("kyoto") is job
    gate.set()


def test_snapshot_has_the_wire_shape_the_polling_endpoint_expects(tmp_path):
    job = TripJob("kyoto", _log(tmp_path))
    snap = job.snapshot()
    assert set(snap) == {"id", "status", "status_version", "kind", "message"}
    assert snap["status"] == "running"
    assert snap["id"] == job.job_id
