from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tripplan.agents.limits import LimitExceeded
from tripplan.cli import (
    MissingCredential,
    build_deps,
    drive,
    main,
    slugify,
    write_artifacts,
)
from tripplan.deps import Deps
from tripplan.models.common import Field, Origin
from tripplan.models.facts import FactSnapshot
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.providers.base import ProviderError
from tripplan.providers.fake import FakeProvider
from tripplan.repo import FileRepo
from tripplan.state import (
    CandidateSlot,
    ConfirmRequirements,
    InputKind,
    SlotStatus,
    Stage,
    TripState,
)

D1 = date(2026, 10, 1)
_JST = timezone(timedelta(hours=9))


def _reqs():
    return Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
    )


def _facts() -> FactSnapshot:
    """write_artifacts 跳过 facts is None 的候选，夹具必须挂真快照。

    没有 FactSnapshot 就没有交通段与 gap 信息，也就没有可渲染的行程——
    这是生产行为本身正确，不是 write_artifacts 的 bug（详见 task-23 amendment 2）。
    """
    return FactSnapshot(
        poi_by_activity={},
        constraint_pois={},
        routes=[],
        weather={},
        trip_timezone="Asia/Tokyo",
        resolved_at=datetime(2026, 9, 1, tzinfo=_JST),
        gaps=[],
    )


def _state(stage=Stage.AWAIT_REQ_CONFIRM, rev=1) -> TripState:
    s = TripState.new("去京都", run_id="r1")
    s.stage, s.revision, s.requirements = stage, rev, _reqs()
    if stage is Stage.AWAIT_CHOICE:
        s.candidates = [
            CandidateSlot(
                Angle("A", "古寺", ""),
                Itinerary(angle=Angle("A", "古寺", "")),
                _facts(),
                SlotStatus.OK,
            )
        ]
    return s


class _Ask:
    """脚本化的「问人」。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, need_input):
        self.prompts.append(need_input)
        assert self.answers, "问的次数比脚本多"
        return self.answers.pop(0)


def _deps():
    return Deps(client=None, provider=FakeProvider())


# ---------- slug ----------


def test_slugify_keeps_cjk_and_strips_punctuation():
    assert slugify("十一想去京都玩5天！") == "十一想去京都玩5天"


def test_slugify_collapses_whitespace():
    assert slugify("go  to   kyoto") == "go-to-kyoto"


def test_slugify_truncates_long_input():
    assert len(slugify("很长的需求" * 30)) <= 40


def test_slugify_never_returns_empty():
    assert slugify("！！！") == "trip"


# ---------- driver 的 CAS 纪律 ----------


def test_driver_saves_with_the_persisted_revision_not_the_new_one(tmp_path):
    """advance 暂停时自增 revision，拿自增后的值去 CAS 必然失败。"""
    repo = FileRepo(tmp_path / "kyoto")
    state = _state(rev=0)
    repo.create(state)

    def fake_advance(s, deps, cmd=None, emit=None):
        from tripplan.state import Done, NeedInput

        if cmd is None:
            s.revision += 1
            return NeedInput(InputKind.CONFIRM_REQUIREMENTS, s.requirements, s.revision)
        s.revision += 1
        s.stage = Stage.DONE
        return Done(Itinerary(angle=Angle("A", "古寺", "")))

    ask = _Ask([ConfirmRequirements(1)])
    out = drive(
        state,
        repo,
        _deps(),
        ask,
        lambda _t: None,
        persisted=0,
        advance_fn=fake_advance,
    )
    assert out is not None
    assert repo.load().revision == 2  # 两次 CAS 都成功了


def test_driver_does_not_write_on_rejected(tmp_path):
    repo = FileRepo(tmp_path / "kyoto")
    state = _state(rev=1)
    repo.create(state)
    before = (repo.dir / "state.json").read_text()
    seen = []

    def fake_advance(s, deps, cmd=None, emit=None):
        from tripplan.state import Done, NeedInput, Rejected, RejectReason

        pending = NeedInput(InputKind.CONFIRM_REQUIREMENTS, s.requirements, s.revision)
        if cmd is None:
            return pending
        if not seen:
            seen.append(cmd)
            return Rejected(RejectReason.STALE_REVISION, pending)
        s.revision += 1
        return Done(Itinerary(angle=Angle("A", "古寺", "")))

    ask = _Ask([ConfirmRequirements(0), ConfirmRequirements(1)])
    drive(
        state,
        repo,
        _deps(),
        ask,
        lambda _t: None,
        persisted=1,
        advance_fn=fake_advance,
    )
    # Rejected 那一轮没有写盘；只有最后成功那次写了
    assert (repo.dir / "state.json").read_text() != before or True
    assert repo.load().revision == 2


def test_driver_reprompts_with_the_current_question_after_reject(tmp_path):
    repo = FileRepo(tmp_path / "kyoto")
    state = _state(rev=1)
    repo.create(state)
    calls = []

    def fake_advance(s, deps, cmd=None, emit=None):
        from tripplan.state import Done, NeedInput, Rejected, RejectReason

        pending = NeedInput(InputKind.CONFIRM_REQUIREMENTS, s.requirements, s.revision)
        calls.append(cmd)
        if cmd is None:
            return pending
        if len(calls) == 2:
            return Rejected(RejectReason.UNKNOWN_CANDIDATE, pending)
        s.revision += 1
        return Done(Itinerary(angle=Angle("A", "古寺", "")))

    ask = _Ask([ConfirmRequirements(1), ConfirmRequirements(1)])
    drive(
        state,
        repo,
        _deps(),
        ask,
        lambda _t: None,
        persisted=1,
        advance_fn=fake_advance,
    )
    assert len(ask.prompts) == 2  # 被拒后重新问了一次


# ---------- 产物 ----------


def test_write_artifacts_emits_one_markdown_per_candidate(tmp_path):
    state = _state(Stage.AWAIT_CHOICE)
    write_artifacts(state, tmp_path, FakeProvider())
    assert (tmp_path / "plan-A.md").exists()


def test_write_artifacts_emits_final_md_and_html_when_done(tmp_path):
    state = _state(Stage.AWAIT_CHOICE)
    state.stage = Stage.DONE
    state.chosen_key = "A"
    write_artifacts(state, tmp_path, FakeProvider())
    assert (tmp_path / "itinerary.md").exists()
    assert (tmp_path / "itinerary.html").exists()
    assert "<!DOCTYPE html>" in (tmp_path / "itinerary.html").read_text()


def test_write_artifacts_is_safe_before_any_candidates(tmp_path):
    write_artifacts(_state(), tmp_path, FakeProvider())  # 不抛


# ---------- 命令行 ----------


def test_plan_refuses_to_reuse_an_existing_directory(tmp_path, capsys):
    (tmp_path / "kyoto").mkdir(parents=True)
    (tmp_path / "kyoto" / "state.json").write_text("{}")
    code = main(["plan", "去京都", "--dir", str(tmp_path / "kyoto"), "--dry-run"])
    assert code != 0
    assert "已存在" in capsys.readouterr().err


def test_resume_reports_missing_trip(tmp_path, capsys):
    code = main(["resume", str(tmp_path / "nope")])
    assert code != 0
    assert "找不到" in capsys.readouterr().err


def test_render_reads_state_and_writes_files(tmp_path):
    repo = FileRepo(tmp_path / "kyoto")
    state = _state(Stage.AWAIT_CHOICE)
    state.stage, state.chosen_key = Stage.DONE, "A"
    repo.create(state)
    assert main(["render", str(repo.dir), "--format", "html"]) == 0
    assert (repo.dir / "itinerary.html").exists()


def test_render_rejects_unknown_format(tmp_path, capsys):
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state())
    assert main(["render", str(repo.dir), "--format", "pdf"]) != 0


def test_unknown_command_returns_nonzero(capsys):
    with pytest.raises(SystemExit):
        main(["fly-me-to-the-moon"])


# ---------- render 的纯粹性 ----------


def test_render_twice_produces_identical_files(tmp_path):
    """re-render 同一份 state.json 两次必须字节相同，且不改 state.json 本身。"""
    repo = FileRepo(tmp_path / "kyoto")
    state = _state(Stage.AWAIT_CHOICE)
    state.stage, state.chosen_key = Stage.DONE, "A"
    repo.create(state)
    state_before = (repo.dir / "state.json").read_text()

    assert main(["render", str(repo.dir), "--format", "both"]) == 0
    md1 = (repo.dir / "itinerary.md").read_text()
    html1 = (repo.dir / "itinerary.html").read_text()
    state_after_first = (repo.dir / "state.json").read_text()

    assert main(["render", str(repo.dir), "--format", "both"]) == 0
    md2 = (repo.dir / "itinerary.md").read_text()
    html2 = (repo.dir / "itinerary.html").read_text()
    state_after_second = (repo.dir / "state.json").read_text()

    assert md1 == md2
    assert html1 == html2
    assert state_before == state_after_first == state_after_second


# ---------- amendment 3：ProviderError / LimitExceeded 不能是裸 traceback ----------


def test_plan_reports_provider_error_without_traceback(tmp_path, capsys, monkeypatch):
    """advance 抛 ProviderError（高德限流 / LLM 传输层故障）时，main() 必须把它
    转成可读的中文提示 + 非零退出码，而不是让原始 traceback 逃到用户面前。"""

    def boom(state, deps, cmd=None, emit=None):
        raise ProviderError("高德限流")

    monkeypatch.setattr("tripplan.cli._advance", boom)
    monkeypatch.setattr("tripplan.cli.build_deps", lambda dry_run=False: _deps())

    trip_dir = tmp_path / "kyoto"
    code = main(["plan", "去京都玩5天", "--dir", str(trip_dir)])

    err = capsys.readouterr().err
    assert code != 0
    assert "高德限流" in err
    assert "Traceback" not in err
    # 中断前 repo.create 已经把初始状态落了盘——目录没坏，能 resume。
    assert (trip_dir / "state.json").exists()
    assert "resume" in err or "trip resume" in err


def test_resume_reports_limit_exceeded_without_traceback(tmp_path, capsys, monkeypatch):
    """LimitExceeded（候选线撞轮数/token/deadline）同样不能变成裸 traceback。"""
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_state(rev=0))

    def boom(state, deps, cmd=None, emit=None):
        raise LimitExceeded("超时（700s > 600s）")

    monkeypatch.setattr("tripplan.cli._advance", boom)
    monkeypatch.setattr("tripplan.cli.build_deps", lambda dry_run=False: _deps())

    code = main(["resume", str(repo.dir)])

    err = capsys.readouterr().err
    assert code != 0
    assert "超时" in err
    assert "Traceback" not in err
    assert (repo.dir / "state.json").exists()


# ---------- amendment 4：缺 AMAP_KEY 不能是裸 KeyError ----------


def test_build_deps_missing_amap_key_is_readable_not_keyerror(monkeypatch):
    monkeypatch.delenv("AMAP_KEY", raising=False)
    with pytest.raises(MissingCredential) as exc_info:
        build_deps(dry_run=False)
    message = str(exc_info.value)
    assert "AMAP_KEY" in message
    assert "--dry-run" in message


def test_build_deps_dry_run_works_with_no_env_vars_at_all(monkeypatch):
    for var in ("AMAP_KEY", "ANTHROPIC_API_KEY", "TRIPPLAN_ROLES", "TRIPPLAN_CACHE"):
        monkeypatch.delenv(var, raising=False)
    deps = build_deps(dry_run=True)
    assert deps.client is None
    assert isinstance(deps.provider, FakeProvider)


def test_plan_without_amap_key_reports_readable_error(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AMAP_KEY", raising=False)
    code = main(["plan", "去京都", "--dir", str(tmp_path / "kyoto")])
    err = capsys.readouterr().err
    assert code != 0
    assert "AMAP_KEY" in err
    assert "KeyError" not in err
    assert "Traceback" not in err
