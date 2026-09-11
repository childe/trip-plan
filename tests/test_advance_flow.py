from datetime import date

import pytest

from tripplan.agents.limits import LimitExceeded
from tripplan.agents.steps import FeedbackDelta, Scale
from tripplan.deps import Deps
from tripplan.models.common import Field, Origin
from tripplan.models.issue import Issue
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.orchestrator import advance
from tripplan.providers.base import ProviderError
from tripplan.providers.fake import FakeProvider
from tripplan.state import (
    AmendRequirements,
    CandidateSlot,
    ChooseCandidate,
    ConfirmRequirements,
    Done,
    GiveFeedback,
    InputKind,
    NeedInput,
    Rejected,
    SlotStatus,
    Stage,
    TripState,
)
from tripplan.wire import dumps

D1 = date(2026, 10, 1)


def _reqs(dest="京都"):
    return Requirements(
        destination=Field(dest, Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
    )


def _itin(key="A"):
    return Itinerary(angle=Angle(key, f"方案{key}", ""))


class _Fakes:
    """替换 orchestrator 依赖的四个外部步骤，只留控制流。"""

    def __init__(self, delta=None, angles=("A", "B", "C")):
        self.delta = delta
        self.angle_keys = list(angles)
        self.collect_calls = 0
        self.slot_calls = []
        self.classify_calls = 0

    def collect(self, raw_request, deps, ctx=None):
        self.collect_calls += 1
        return _reqs()

    def pick_angles(self, reqs, deps, ctx=None, n=3):
        return [Angle(k, f"方案{k}", "") for k in self.angle_keys]

    def run_slot(self, angle, seed, reqs, tz, deps, **kw):
        self.slot_calls.append(
            {
                "angle": angle.key,
                "seed": seed,
                "tz": tz,
                "issues": list(kw.get("issues", ())),
            }
        )
        return CandidateSlot(angle, _itin(angle.key), None, SlotStatus.OK)

    def classify_feedback(self, text, reqs, deps, ctx=None):
        self.classify_calls += 1
        return self.delta or FeedbackDelta(False, {}, Scale.INCREMENTAL)


@pytest.fixture
def wire(monkeypatch):
    def _install(fakes):
        import tripplan.orchestrator as orch

        monkeypatch.setattr(orch, "collect", fakes.collect)
        monkeypatch.setattr(orch, "pick_angles", fakes.pick_angles)
        monkeypatch.setattr(orch, "run_slot", fakes.run_slot)
        monkeypatch.setattr(orch, "classify_feedback", fakes.classify_feedback)
        return fakes

    return _install


def _deps():
    return Deps(client=None, provider=FakeProvider())


def _at_choice(chosen=None):
    s = TripState.new("去京都", run_id="r1")
    s.stage = Stage.AWAIT_CHOICE
    s.revision = 5
    s.requirements = _reqs()
    s.trip_timezone = "Asia/Tokyo"
    s.candidates = [
        CandidateSlot(Angle(k, f"方案{k}", ""), _itin(k), None, SlotStatus.OK)
        for k in ("A", "B", "C")
    ]
    s.chosen_key = chosen
    return s


# ---------- COLLECT ----------


def test_first_advance_collects_then_pauses(wire):
    fakes = wire(_Fakes())
    s = TripState.new("去京都5天", run_id="r1")
    out = advance(s, _deps())
    assert isinstance(out, NeedInput)
    assert s.stage is Stage.AWAIT_REQ_CONFIRM
    assert fakes.collect_calls == 1
    assert s.revision == 1


def test_confirm_marks_fields_confirmed_and_generates(wire):
    fakes = wire(_Fakes())
    s = TripState.new("去京都", run_id="r1")
    advance(s, _deps())
    advance(s, _deps(), ConfirmRequirements(s.revision))
    assert s.requirements.destination.confirmed is True
    assert [c.angle.key for c in s.candidates] == ["A", "B", "C"]
    assert s.stage is Stage.AWAIT_CHOICE


def test_amend_at_confirm_reruns_collect(wire):
    fakes = wire(_Fakes())
    s = TripState.new("去京都", run_id="r1")
    advance(s, _deps())
    advance(s, _deps(), AmendRequirements(s.revision, "预算1万5"))
    assert fakes.collect_calls == 2
    assert "预算1万5" in s.raw_request


# ---------- 时区 ----------


def test_timezone_resolved_before_generating(wire):
    fakes = wire(_Fakes())
    s = TripState.new("去京都", run_id="r1")
    advance(s, _deps())
    advance(s, _deps(), ConfirmRequirements(s.revision))
    assert s.trip_timezone == "Asia/Tokyo"
    assert all(c["tz"] == "Asia/Tokyo" for c in fakes.slot_calls)


# ---------- revision 二分律 ----------


def test_choose_candidate_bumps_revision(wire):
    """P0：DONE 路径不经过工作态，递增点必须覆盖它，否则 CAS 形同虚设。"""
    wire(_Fakes())
    s = _at_choice()
    before = s.revision
    out = advance(s, _deps(), ChooseCandidate(before, "A"))
    assert isinstance(out, Done)
    assert s.revision == before + 1


def test_two_concurrent_choices_cannot_both_commit(wire, tmp_path):
    """还原 review 描述的场景：都读到 rev=5，分别选 A 和 B。"""
    from tripplan.repo import FileRepo

    wire(_Fakes())
    repo = FileRepo(tmp_path / "kyoto")
    repo.create(_at_choice())

    a, b = repo.load(), repo.load()
    persisted = 5

    advance(a, _deps(), ChooseCandidate(5, "A"))
    assert repo.save_if_revision(a, persisted) is True

    advance(b, _deps(), ChooseCandidate(5, "B"))
    assert repo.save_if_revision(b, persisted) is False  # ★ 被挡住
    assert repo.load().chosen_key == "A"


@pytest.mark.parametrize(
    "cmd_factory",
    [
        lambda s: ConfirmRequirements(s.revision),
        lambda s: AmendRequirements(s.revision, "改成四天"),
        lambda s: ChooseCandidate(s.revision, "A"),
        lambda s: GiveFeedback(s.revision, "A", "第2天太赶"),
    ],
)
def test_every_accepted_command_bumps_revision_exactly_once(wire, cmd_factory):
    wire(_Fakes())
    s = _at_choice()
    if isinstance(cmd_factory(s), ConfirmRequirements):
        s.stage = Stage.AWAIT_REQ_CONFIRM
    before = s.revision
    out = advance(s, _deps(), cmd_factory(s))
    assert not isinstance(out, Rejected)
    assert s.revision == before + 1


# ---------- 反馈分类只发生一次 ----------


def test_classify_feedback_is_called_exactly_once(wire):
    """两次分类结果可能不一致，状态会就此走歪且无人报错。"""
    fakes = wire(
        _Fakes(
            delta=FeedbackDelta(
                True,
                {"dates": {"start": "2026-10-01", "end": "2026-10-04"}},
                Scale.INCREMENTAL,
            )
        )
    )
    s = _at_choice()
    advance(s, _deps(), GiveFeedback(s.revision, "A", "改成四天"))
    assert fakes.classify_calls == 1


# ---------- 改行程 vs 改需求 ----------


def test_itinerary_feedback_selects_the_slot_and_refines(wire):
    fakes = wire(_Fakes(delta=FeedbackDelta(False, {}, Scale.INCREMENTAL)))
    s = _at_choice()
    advance(s, _deps(), GiveFeedback(s.revision, "B", "第2天太赶了"))
    assert s.chosen_key == "B"
    assert [c["angle"] for c in fakes.slot_calls] == ["B"]  # 只跑选中那份
    assert any("太赶" in i.message for i in fakes.slot_calls[0]["issues"])


def test_requirement_change_before_selection_reruns_all_three(wire):
    fakes = wire(
        _Fakes(
            delta=FeedbackDelta(
                True,
                {"dates": {"start": "2026-10-01", "end": "2026-10-04"}},
                Scale.INCREMENTAL,
            )
        )
    )
    s = _at_choice()
    advance(s, _deps(), AmendRequirements(s.revision, "改成四天"))
    assert sorted(c["angle"] for c in fakes.slot_calls) == ["A", "B", "C"]


def test_incremental_change_passes_previous_itineraries_as_seeds(wire):
    """三天改四天时用户对前三天可能已满意，从零重规划会洗掉它。"""
    fakes = wire(
        _Fakes(
            delta=FeedbackDelta(
                True,
                {"dates": {"start": "2026-10-01", "end": "2026-10-04"}},
                Scale.INCREMENTAL,
            )
        )
    )
    s = _at_choice()
    advance(s, _deps(), AmendRequirements(s.revision, "改成四天"))
    assert all(c["seed"] is not None for c in fakes.slot_calls)


def test_old_issues_are_discarded_when_requirements_change(wire):
    fakes = wire(
        _Fakes(
            delta=FeedbackDelta(
                True,
                {"dates": {"start": "2026-10-01", "end": "2026-10-04"}},
                Scale.INCREMENTAL,
            )
        )
    )
    s = _at_choice()
    s.issues = [Issue.from_human("老意见")]
    advance(s, _deps(), AmendRequirements(s.revision, "改成四天"))
    assert all(not c["issues"] for c in fakes.slot_calls)


# ---------- REWRITE ----------


def test_rewrite_clears_itineraries_but_keeps_angles(wire):
    """P0：换目的地不能拿旧行程当 seed。角度保留——用户已表达过这个偏好。"""
    fakes = wire(
        _Fakes(delta=FeedbackDelta(True, {"destination": "巴黎"}, Scale.REWRITE))
    )
    s = _at_choice()
    advance(s, _deps(), AmendRequirements(s.revision, "改去巴黎"))
    assert all(c["seed"] is None for c in fakes.slot_calls)
    assert sorted(c["angle"] for c in fakes.slot_calls) == ["A", "B", "C"]


def test_rewrite_recomputes_timezone(wire):
    """P0：trip_timezone 原先只在 GENERATE 重算，会一路停在 Asia/Tokyo。"""
    fakes = wire(
        _Fakes(delta=FeedbackDelta(True, {"destination": "巴黎"}, Scale.REWRITE))
    )
    s = _at_choice()
    advance(s, _deps(), AmendRequirements(s.revision, "改去巴黎"))
    assert s.trip_timezone == "Europe/Paris"
    assert all(c["tz"] == "Europe/Paris" for c in fakes.slot_calls)


def test_rewrite_after_selection_still_clears_the_chosen_slot(wire):
    """这正是原设计漏掉的分支：已选定时直接进 REFINE，带着旧行程。"""
    fakes = wire(
        _Fakes(delta=FeedbackDelta(True, {"destination": "巴黎"}, Scale.REWRITE))
    )
    s = _at_choice(chosen="B")
    advance(s, _deps(), GiveFeedback(s.revision, "B", "改去巴黎"))
    assert [c["angle"] for c in fakes.slot_calls] == ["B"]
    assert fakes.slot_calls[0]["seed"] is None
    assert fakes.slot_calls[0]["tz"] == "Europe/Paris"


def test_destination_change_marked_incremental_still_recomputes_timezone(wire):
    fakes = wire(
        _Fakes(delta=FeedbackDelta(True, {"destination": "巴黎"}, Scale.INCREMENTAL))
    )
    s = _at_choice()
    advance(s, _deps(), AmendRequirements(s.revision, "改去巴黎"))
    assert s.trip_timezone == "Europe/Paris"


# ---------- 定稿 ----------


def test_done_returns_the_chosen_itinerary(wire):
    wire(_Fakes())
    s = _at_choice()
    out = advance(s, _deps(), ChooseCandidate(s.revision, "C"))
    assert isinstance(out, Done)
    assert out.itinerary.angle.key == "C"
    assert s.stage is Stage.DONE


# ---------- 中断续跑 ----------


def test_state_survives_a_serialization_roundtrip_mid_flow(wire):
    """任意暂停点存盘、丢弃内存对象、重新载入，后续行为一致。"""
    from tripplan.wire import loads

    wire(_Fakes())
    s = TripState.new("去京都", run_id="r1")
    advance(s, _deps())

    revived = loads(dumps(s))
    assert dumps(revived) == dumps(s)

    out_a = advance(s, _deps(), ConfirmRequirements(s.revision))
    out_b = advance(revived, _deps(), ConfirmRequirements(revived.revision))
    assert dumps(s) == dumps(revived)
    assert type(out_a) is type(out_b)


# ---------- 携带的问题 1：候选并发容错 ----------
#
# run_slot 自己只兜 LimitExceeded / ProviderError。任何跑漏的异常（一个未
# 预见的 bug、以后新长出的失败模式……）都不能把另外两条已经跑完的候选一起
# 拖垮——GENERATE 里目前是 list comprehension 顺序跑，一条线抛出去足以
# 让整个 advance 都崩掉。


def test_candidate_fanout_contains_a_single_slot_crash(wire, monkeypatch):
    import tripplan.orchestrator as orch

    wire(_Fakes())

    def _crashy_run_slot(angle, seed, reqs, tz, deps, **kw):
        if angle.key == "B":
            raise RuntimeError("kaboom")
        return CandidateSlot(angle, _itin(angle.key), None, SlotStatus.OK)

    monkeypatch.setattr(orch, "run_slot", _crashy_run_slot)

    s = TripState.new("去京都", run_id="r1")
    advance(s, _deps())
    out = advance(s, _deps(), ConfirmRequirements(s.revision))  # 不应该抛异常

    assert isinstance(out, NeedInput)
    assert s.slot("A").status is SlotStatus.OK
    assert s.slot("C").status is SlotStatus.OK
    b = s.slot("B")
    assert b.status is SlotStatus.FAILED
    assert b.itinerary is None
    assert "kaboom" in b.detail


def test_diversity_retry_callback_contains_exceptions(wire, monkeypatch):
    """enforce_diversity 的 regenerate 回调同样要兜住——它也是一条候选线的
    run_slot 调用，只是触发方式不同（差异度太高而不是首次生成）。"""
    import tripplan.orchestrator as orch

    wire(_Fakes())

    def _run_slot_ok_unless_retried(angle, seed, reqs, tz, deps, **kw):
        if angle.key == "B" and kw.get("avoid_poi_ids"):
            raise RuntimeError("retry-boom")
        return CandidateSlot(angle, _itin(angle.key), None, SlotStatus.OK)

    def _fake_enforce_diversity(slots, regenerate, emit=None):
        out = list(slots)
        out[1] = regenerate(out[1], frozenset({"poi-1"}))
        return out

    monkeypatch.setattr(orch, "run_slot", _run_slot_ok_unless_retried)
    monkeypatch.setattr(orch, "enforce_diversity", _fake_enforce_diversity)

    s = TripState.new("去京都", run_id="r1")
    advance(s, _deps())
    out = advance(s, _deps(), ConfirmRequirements(s.revision))  # 不应该抛异常

    assert isinstance(out, NeedInput)
    assert s.slot("A").status is SlotStatus.OK
    b = s.slot("B")
    assert b.status is SlotStatus.FAILED
    assert "retry-boom" in b.detail
    assert s.slot("C").status is SlotStatus.OK


# ---------- 携带的问题 2：pick_angles 的裸 ValueError ----------


def test_pick_angles_failure_is_contained_not_a_raw_exception(wire, monkeypatch):
    import tripplan.orchestrator as orch

    wire(_Fakes())

    def _boom_pick_angles(reqs, deps, ctx=None, n=3):
        raise ValueError("角度 key 重复：['A', 'A']")

    monkeypatch.setattr(orch, "pick_angles", _boom_pick_angles)

    s = TripState.new("去京都", run_id="r1")
    advance(s, _deps())
    out = advance(s, _deps(), ConfirmRequirements(s.revision))  # 不应该抛异常

    assert isinstance(out, NeedInput)
    assert out.kind is InputKind.CHOOSE_OR_FEEDBACK
    assert s.stage is Stage.AWAIT_CHOICE
    assert any(c.status is SlotStatus.FAILED for c in s.candidates)
    assert all(c.itinerary is None for c in s.candidates)


def test_pick_angles_failure_can_be_recovered_via_amend(wire, monkeypatch):
    """角度生成失败之后不是死路——一次带真正补丁的 AmendRequirements 应该
    能重新走到 GENERATE 并成功。"""
    import tripplan.orchestrator as orch

    fakes = wire(
        _Fakes(delta=FeedbackDelta(True, {"destination": "东京"}, Scale.INCREMENTAL))
    )
    calls = {"n": 0}

    def _flaky_pick_angles(reqs, deps, ctx=None, n=3):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("角度生成失败")
        return [Angle(k, f"方案{k}", "") for k in ("A", "B", "C")]

    monkeypatch.setattr(orch, "pick_angles", _flaky_pick_angles)

    s = TripState.new("去京都", run_id="r1")
    advance(s, _deps())
    advance(s, _deps(), ConfirmRequirements(s.revision))
    assert s.stage is Stage.AWAIT_CHOICE
    assert any(c.status is SlotStatus.FAILED for c in s.candidates)

    out = advance(s, _deps(), AmendRequirements(s.revision, "换成东京再试一次"))
    assert isinstance(out, NeedInput)
    assert [c.angle.key for c in s.candidates] == ["A", "B", "C"]
    assert all(c.status is SlotStatus.OK for c in s.candidates)


# ---------- classify_feedback 的空 patch：不能算真正的需求变更 ----------


def test_amend_with_empty_patch_at_await_choice_is_a_noop_but_still_bumps_revision(
    wire,
):
    """patches_requirements=True 但 patch={} 不是真正的需求变更——按它走
    REWRITE/清候选只会白白丢掉已经跑出来的三个候选。"""
    fakes = wire(_Fakes(delta=FeedbackDelta(True, {}, Scale.INCREMENTAL)))
    s = _at_choice()
    before = s.revision
    before_keys = [c.angle.key for c in s.candidates]

    out = advance(s, _deps(), AmendRequirements(before, "随便说点什么"))

    assert not isinstance(out, Rejected)
    assert s.revision == before + 1
    assert s.stage is Stage.AWAIT_CHOICE
    assert [c.angle.key for c in s.candidates] == before_keys
    assert fakes.slot_calls == []


def test_feedback_with_empty_patch_is_treated_as_itinerary_feedback(wire):
    """同样的空 patch 场景放在 GiveFeedback 上：应该退化成对该候选的意见，
    而不是触发一次什么都不改的"需求变更"。"""
    fakes = wire(_Fakes(delta=FeedbackDelta(True, {}, Scale.INCREMENTAL)))
    s = _at_choice()
    advance(s, _deps(), GiveFeedback(s.revision, "B", "颜色不喜欢"))
    assert s.chosen_key == "B"
    assert [c["angle"] for c in fakes.slot_calls] == ["B"]
    assert any("颜色不喜欢" in i.message for i in fakes.slot_calls[0]["issues"])


# ---------- xfail 摘除后的回归 ----------


def test_amend_is_allowed_even_when_required_missing(wire):
    """只有用户补充了新信息才值得重跑 COLLECT。"""
    fakes = wire(_Fakes())
    s = TripState.new("去京都", run_id="r1")
    s.stage = Stage.AWAIT_REQ_CONFIRM
    s.revision = 3
    s.requirements = Requirements()
    out = advance(s, _deps(), AmendRequirements(3, "目的地是京都"))
    assert not isinstance(out, Rejected)


def test_exhausted_candidate_is_selectable(wire):
    """带着遗留硬伤定稿是用户的权利——问题都摆在他面前了。"""
    wire(_Fakes())
    s = _at_choice()
    s.candidates[1].status = SlotStatus.EXHAUSTED
    s.candidates[1].detail = "仍有硬伤"
    out = advance(s, _deps(), ChooseCandidate(s.revision, "B"))
    assert not isinstance(out, Rejected)


# ---------- 协调者复审发现：外部依赖失败必须落在拒绝/修改二分律之外 ----------
#
# collect / classify_feedback 直接触达 LLM；一旦底层传输故障（归一为
# ProviderError）或撞额度（LimitExceeded），既不能被静默吞掉伪装成一次
# 正常的 state 变化，也不能在半路留下部分改动——异常必须原样穿透
# advance，而且穿透之前不能有任何写回 state 的副作用留下痕迹。


def test_classify_feedback_failure_does_not_mutate_chosen_key_or_bump_revision(
    wire, monkeypatch
):
    """复现协调者给出的场景：GiveFeedback 先落 chosen_key 再分类，
    分类失败时 chosen_key 已经被改写，revision 却没有递增。"""
    import tripplan.orchestrator as orch

    wire(_Fakes())

    def _boom_classify(text, reqs, deps, ctx=None):
        raise ProviderError("模型暂时不可用")

    monkeypatch.setattr(orch, "classify_feedback", _boom_classify)

    s = _at_choice()  # chosen_key 初始为 None
    before_rev = s.revision

    with pytest.raises(ProviderError):
        advance(s, _deps(), GiveFeedback(before_rev, "A", "太赶了"))

    assert s.chosen_key is None
    assert s.revision == before_rev


def test_pick_angles_provider_error_is_contained_like_value_error(wire, monkeypatch):
    """pick_angles 的 ProviderError 应该和它自己的裸 ValueError 一样，
    收敛成 FAILED 占位候选，而不是逃出 advance。"""
    import tripplan.orchestrator as orch

    wire(_Fakes())

    def _boom_pick_angles(reqs, deps, ctx=None, n=3):
        raise ProviderError("模型暂时不可达")

    monkeypatch.setattr(orch, "pick_angles", _boom_pick_angles)

    s = TripState.new("去京都", run_id="r1")
    advance(s, _deps())
    out = advance(s, _deps(), ConfirmRequirements(s.revision))  # 不应该抛异常

    assert isinstance(out, NeedInput)
    assert s.stage is Stage.AWAIT_CHOICE
    assert any(c.status is SlotStatus.FAILED for c in s.candidates)


def test_pick_angles_limit_exceeded_is_contained_like_value_error(wire, monkeypatch):
    """同上，换成撞额度的 LimitExceeded。"""
    import tripplan.orchestrator as orch

    wire(_Fakes())

    def _boom_pick_angles(reqs, deps, ctx=None, n=3):
        raise LimitExceeded("schema 修复次数耗尽")

    monkeypatch.setattr(orch, "pick_angles", _boom_pick_angles)

    s = TripState.new("去京都", run_id="r1")
    advance(s, _deps())
    out = advance(s, _deps(), ConfirmRequirements(s.revision))  # 不应该抛异常

    assert isinstance(out, NeedInput)
    assert any(c.status is SlotStatus.FAILED for c in s.candidates)


def test_collect_failure_propagates_and_leaves_state_unmutated(wire, monkeypatch):
    """collect 失败要原样穿透 advance，且不留下任何部分改动——
    这是刻意的第三种结局，不应该被伪装成 Rejected 或 NeedInput。"""
    import tripplan.orchestrator as orch

    wire(_Fakes())

    def _boom_collect(raw_request, deps, ctx=None):
        raise ProviderError("模型暂时不可达")

    monkeypatch.setattr(orch, "collect", _boom_collect)

    s = TripState.new("去京都", run_id="r1")
    before = dumps(s)

    with pytest.raises(ProviderError):
        advance(s, _deps())

    assert dumps(s) == before
    assert s.revision == 0


def test_a_wrongly_typed_angle_key_no_longer_escapes_advance_as_a_typeerror():
    """评审 I6 的回归：{"angles":[{"key":[],...}]} 曾经一路走到
    pick_angles 的 len(set(keys))，抛 TypeError: unhashable type: 'list'——
    那个类型不在 _run_to_pause 的 except 元组里，直接变成一截裸 traceback。

    这里**不**打桩 pick_angles：走真实的 run_agent，让 schema 里早就写着的
    `"key": {"type": "string"}` 自己把它挡回去。挡回去之后模型改不出来，
    最终以 LimitExceeded 收场——那个类型在元组里，于是流程停在一个合法的
    暂停态（FAILED 占位候选），而不是崩给用户。"""
    import json

    from tripplan.llm.client import FakeLlm, LlmResponse, Usage

    bad = LlmResponse(
        "end_turn",
        json.dumps({"angles": [{"key": [], "title": "T"}]}),
        [],
        Usage(10, 10),
    )
    s = TripState.new("去京都", run_id="r1")
    s.stage = Stage.GENERATE
    s.requirements = _reqs()
    s.trip_timezone = "Asia/Tokyo"
    deps = Deps(client=FakeLlm([bad] * 3), provider=FakeProvider())

    out = advance(s, deps)  # 不应该抛

    assert isinstance(out, NeedInput)
    assert s.stage is Stage.AWAIT_CHOICE
    assert [c.status for c in s.candidates] == [SlotStatus.FAILED]
