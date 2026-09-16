"""run_command 的提交纪律（spec §4.1 / §9 回归 7、10、12、14）。

这一层框架无关：advance / stage / publish / discard 都可注入，所以「CAS 输了」
「产物生成失败」「中途取消」这些分支全部可以纯内存地故障注入，不碰 LLM 与高德。
"""

import threading
from datetime import date

import pytest

from tripplan.agents.limits import Cancelled
from tripplan.artifacts import ARTIFACTS_JSON
from tripplan.deps import Deps
from tripplan.models.common import Field, Origin
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.providers.base import ProviderError
from tripplan.providers.fake import FakeProvider
from tripplan.repo import FileRepo
from tripplan.state import (
    CandidateSlot,
    ConfirmRequirements,
    Done,
    InputKind,
    NeedInput,
    Rejected,
    RejectReason,
    SlotStatus,
    Stage,
    TripState,
)
from tripplan.web.jobs import JobOutcome, run_command

D1 = date(2026, 10, 1)


class _FakeJob:
    """run_command 只需要这三样。完整的 TripJob 在 Task 7。"""

    def __init__(self, job_id="job-1"):
        self.job_id = job_id
        self.cancel_token = threading.Event()
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _reqs():
    return Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
    )


def _seed(trips_root, tid="kyoto", stage=Stage.AWAIT_REQ_CONFIRM, rev=1):
    repo = FileRepo(trips_root / tid)
    state = TripState.new("去京都", run_id="r1")
    state.stage, state.revision, state.requirements = stage, rev, _reqs()
    angle = Angle("A", "古寺", "")
    state.candidates = [
        CandidateSlot(angle, Itinerary(angle=angle), None, SlotStatus.OK)
    ]
    repo.create(state)
    return repo


def _pending(state):
    return NeedInput(InputKind.CONFIRM_REQUIREMENTS, state.requirements, state.revision)


def test_a_successful_command_advances_once_and_saves(tmp_path):
    repo = _seed(tmp_path)
    calls = []

    def fake_advance(state, deps, cmd=None, emit=None, cancel=None):
        calls.append(cmd)
        state.revision += 1
        return _pending(state)

    job = _FakeJob()
    outcome = run_command(
        tmp_path,
        "kyoto",
        ConfirmRequirements(1),
        Deps(client=None, provider=FakeProvider()),
        job,
        advance_fn=fake_advance,
    )

    assert outcome.status == "succeeded"
    assert outcome.revision == 2
    assert repo.load().revision == 2
    assert len(calls) == 1


def test_state_is_reread_from_disk_every_time(tmp_path):
    """「每次从盘上重读 state」让命令之间完全无内存状态：CAS 纪律自动成立，
    服务器重启后的接续也自动成立（spec §4.1）。"""
    _seed(tmp_path, rev=5)
    seen = []

    def fake_advance(state, deps, cmd=None, emit=None, cancel=None):
        seen.append(state.revision)
        state.revision += 1
        return _pending(state)

    run_command(
        tmp_path,
        "kyoto",
        None,
        Deps(client=None, provider=FakeProvider()),
        _FakeJob(),
        advance_fn=fake_advance,
    )
    assert seen == [5]


def test_rejected_is_not_an_http_error_and_never_hits_the_disk(tmp_path):
    """§9 回归 10：state 未变、revision 未变 → 不落盘。写盘只会白占一次
    CAS 窗口，让无辜的并发调用被误杀。"""
    repo = _seed(tmp_path, rev=1)
    saves = []
    original = FileRepo.save_if_revision

    def counting(self, state, expected):
        saves.append(expected)
        return original(self, state, expected)

    def fake_advance(state, deps, cmd=None, emit=None, cancel=None):
        return Rejected(RejectReason.STALE_REVISION, _pending(state))

    import tripplan.repo

    tripplan.repo.FileRepo.save_if_revision = counting
    try:
        outcome = run_command(
            tmp_path,
            "kyoto",
            ConfirmRequirements(0),
            Deps(client=None, provider=FakeProvider()),
            _FakeJob(),
            advance_fn=fake_advance,
        )
    finally:
        tripplan.repo.FileRepo.save_if_revision = original

    assert outcome.status == "rejected"
    assert outcome.message == RejectReason.STALE_REVISION.value
    assert saves == []
    assert repo.load().revision == 1


def test_losing_the_cas_race_never_publishes_artifacts(tmp_path):
    """§9 回归 7 + 14：stage_artifacts 成功但 CAS 返回 False 时，行程目录里
    **不出现** itinerary.html——盘上的结局不是我们手上这份。"""
    _seed(tmp_path, stage=Stage.AWAIT_CHOICE, rev=1)
    published = []

    def fake_advance(state, deps, cmd=None, emit=None, cancel=None):
        state.stage, state.chosen_key = Stage.DONE, "A"
        state.revision += 1
        return Done(state.chosen().itinerary)

    class _AlwaysLoses(FileRepo):
        def save_if_revision(self, state, expected):
            return False

    outcome = run_command(
        tmp_path,
        "kyoto",
        None,
        Deps(client=None, provider=FakeProvider()),
        _FakeJob(),
        advance_fn=fake_advance,
        open_fn=lambda root, tid: _AlwaysLoses(root / tid),
        publish_fn=published.append,
    )

    assert outcome.status == "failed"
    assert outcome.kind == "Conflict"
    assert published == []
    assert not (tmp_path / "kyoto" / "itinerary.html").exists()
    assert not (tmp_path / "kyoto" / ARTIFACTS_JSON).exists()


def test_a_won_cas_publishes_and_the_manifest_matches_state(tmp_path):
    """§9 回归 14 的正面：CAS 成功时文件出现，且 artifacts.json.revision
    与 state.revision 对齐。"""
    import json

    repo = _seed(tmp_path, stage=Stage.AWAIT_CHOICE, rev=1)

    def fake_advance(state, deps, cmd=None, emit=None, cancel=None):
        state.stage, state.chosen_key = Stage.DONE, "A"
        state.candidates[0].facts = _facts_stub()
        state.revision += 1
        return Done(state.chosen().itinerary)

    outcome = run_command(
        tmp_path,
        "kyoto",
        None,
        Deps(client=None, provider=FakeProvider()),
        _FakeJob(),
        advance_fn=fake_advance,
    )

    assert outcome.status == "succeeded"
    assert (tmp_path / "kyoto" / "itinerary.html").exists()
    manifest = json.loads(
        (tmp_path / "kyoto" / ARTIFACTS_JSON).read_text(encoding="utf-8")
    )
    assert manifest["revision"] == repo.load().revision


def _facts_stub():
    from datetime import datetime, timedelta, timezone

    from tripplan.models.facts import FactSnapshot

    return FactSnapshot(
        poi_by_activity={},
        constraint_pois={},
        routes=[],
        weather={},
        trip_timezone="Asia/Tokyo",
        resolved_at=datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=9))),
        gaps=[],
    )


def test_cancelled_does_not_touch_the_disk(tmp_path):
    """取消不是一种规划结果：不落盘，盘上仍是上一个暂停点（spec §4.1）。"""
    repo = _seed(tmp_path, rev=3)

    def cancelling_advance(state, deps, cmd=None, emit=None, cancel=None):
        state.revision += 1  # 已经改了内存里的 state，但仍然不许落盘
        raise Cancelled("已取消")

    outcome = run_command(
        tmp_path,
        "kyoto",
        None,
        Deps(client=None, provider=FakeProvider()),
        _FakeJob(),
        advance_fn=cancelling_advance,
    )

    assert outcome.status == "cancelled"
    assert repo.load().revision == 3


def test_cancelling_between_staging_and_cas_discards_the_artifacts(tmp_path):
    """§9 回归 12：CAS 前那道取消检查命中时，publish 未被调用、discard 被调用。"""
    _seed(tmp_path, stage=Stage.AWAIT_CHOICE, rev=1)
    job = _FakeJob()
    published, discarded = [], []

    def fake_advance(state, deps, cmd=None, emit=None, cancel=None):
        state.stage, state.chosen_key = Stage.DONE, "A"
        state.candidates[0].facts = _facts_stub()
        state.revision += 1
        return Done(state.chosen().itinerary)

    def staging_then_cancel(state, trip_dir, provider, stage_id, fmt="both"):
        job.cancel_token.set()  # 暂存刚做完，用户点了取消
        return "STAGED"

    outcome = run_command(
        tmp_path,
        "kyoto",
        None,
        Deps(client=None, provider=FakeProvider()),
        job,
        advance_fn=fake_advance,
        stage_fn=staging_then_cancel,
        publish_fn=published.append,
        discard_fn=discarded.append,
    )

    assert outcome.status == "cancelled"
    assert published == []
    assert discarded == ["STAGED"]
    assert not (tmp_path / "kyoto" / "itinerary.html").exists()


def test_provider_and_limit_errors_become_failed_outcomes(tmp_path):
    """接的正是 cli.main() 原来那两个 except 的职责（spec §4.1）。"""
    from tripplan.agents.limits import LimitExceeded

    for exc, kind in (
        (ProviderError("高德限流"), "ProviderError"),
        (LimitExceeded("超时（700s > 600s）"), "LimitExceeded"),
    ):
        _seed(tmp_path, tid=kind.lower())

        def boom(state, deps, cmd=None, emit=None, cancel=None, _e=exc):
            raise _e

        outcome = run_command(
            tmp_path,
            kind.lower(),
            None,
            Deps(client=None, provider=FakeProvider()),
            _FakeJob(),
            advance_fn=boom,
        )
        assert outcome.status == "failed"
        assert outcome.kind == kind
        assert str(exc) in outcome.message


def test_staging_failure_is_recorded_but_does_not_block_the_cas(tmp_path):
    """产物生成慢也可能失败；失败只记进 JobOutcome，绝不影响 CAS 判定
    （spec §4.1）——否则拉图超时会让一次已经付过钱的规划整个作废。"""
    repo = _seed(tmp_path, stage=Stage.AWAIT_CHOICE, rev=1)

    def fake_advance(state, deps, cmd=None, emit=None, cancel=None):
        state.stage, state.chosen_key = Stage.DONE, "A"
        state.revision += 1
        return Done(state.chosen().itinerary)

    def exploding_stage(state, trip_dir, provider, stage_id, fmt="both"):
        raise OSError("磁盘满")

    outcome = run_command(
        tmp_path,
        "kyoto",
        None,
        Deps(client=None, provider=FakeProvider()),
        _FakeJob(),
        advance_fn=fake_advance,
        stage_fn=exploding_stage,
    )

    assert repo.load().revision == 2  # CAS 照常发生
    assert outcome.status == "succeeded"
    assert outcome.kind == "ArtifactError"  # 但如实记下产物没做出来
    assert "磁盘满" in outcome.message


def test_a_no_op_advance_does_not_waste_a_cas_window(tmp_path):
    """cmd=None 落在等待态时 advance 只是重新问一遍，revision 没动。
    再写一次盘只会白占一次 CAS 窗口（cli.drive 里同一处的理由）。"""
    _seed(tmp_path, rev=4)
    saves = []
    original = FileRepo.save_if_revision

    def counting(self, state, expected):
        saves.append(expected)
        return original(self, state, expected)

    import tripplan.repo

    tripplan.repo.FileRepo.save_if_revision = counting
    try:
        outcome = run_command(
            tmp_path,
            "kyoto",
            None,
            Deps(client=None, provider=FakeProvider()),
            _FakeJob(),
            advance_fn=lambda s, d, cmd=None, emit=None, cancel=None: _pending(s),
        )
    finally:
        tripplan.repo.FileRepo.save_if_revision = original

    assert outcome.status == "succeeded"
    assert saves == []


def test_a_missing_or_corrupt_trip_becomes_a_failed_outcome(tmp_path):
    outcome = run_command(
        tmp_path, "nope", None, Deps(client=None, provider=FakeProvider()), _FakeJob()
    )
    assert outcome.status == "failed"
    assert outcome.kind == "TripNotFound"


def test_rebuild_artifacts_publishes_without_touching_state(tmp_path):
    """spec §6.2：不碰 LLM，与 trip render 同一条代码路径，revision 不变。"""
    import json

    from tripplan.web.jobs import rebuild_artifacts

    repo = _seed(tmp_path, stage=Stage.AWAIT_CHOICE, rev=6)
    state = repo.load()
    state.stage, state.chosen_key = Stage.DONE, "A"
    state.candidates[0].facts = _facts_stub()
    repo.save_if_revision(state, 6)

    outcome = rebuild_artifacts(
        tmp_path, "kyoto", Deps(client=None, provider=FakeProvider()), _FakeJob()
    )

    assert outcome.status == "succeeded"
    assert repo.load().revision == 6
    assert (
        json.loads((tmp_path / "kyoto" / ARTIFACTS_JSON).read_text(encoding="utf-8"))[
            "revision"
        ]
        == 6
    )
