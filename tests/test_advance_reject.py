import json

import pytest

from tripplan.deps import Deps
from tripplan.llm.client import FakeLlm, LlmResponse, Usage
from tripplan.llm.config import Role
from tripplan.models.common import Field, Origin
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements
from tripplan.orchestrator import advance
from tripplan.providers.fake import FakeProvider
from tripplan.state import (
    AWAITING,
    AmendRequirements,
    CandidateSlot,
    ChooseCandidate,
    ConfirmRequirements,
    Done,
    GiveFeedback,
    InputKind,
    NeedInput,
    Rejected,
    RejectReason,
    SlotStatus,
    Stage,
    TripState,
)
from tripplan.wire import dumps

from datetime import date

D1 = date(2026, 10, 1)


def _deps():
    return Deps(client=None, provider=FakeProvider())


def _full_reqs():
    return Requirements(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(D1, D1), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
    )


def _awaiting_confirm(reqs=None) -> TripState:
    s = TripState.new("去京都", run_id="r1")
    s.stage = Stage.AWAIT_REQ_CONFIRM
    s.revision = 3
    s.requirements = reqs if reqs is not None else _full_reqs()
    return s


def _awaiting_choice() -> TripState:
    s = TripState.new("去京都", run_id="r1")
    s.stage = Stage.AWAIT_CHOICE
    s.revision = 5
    s.requirements = _full_reqs()
    s.candidates = [
        CandidateSlot(
            Angle("A", "古寺", ""),
            Itinerary(angle=Angle("A", "古寺", "")),
            status=SlotStatus.OK,
        ),
        CandidateSlot(
            Angle("B", "美食", ""),
            Itinerary(angle=Angle("B", "美食", "")),
            status=SlotStatus.EXHAUSTED,
            detail="仍有硬伤",
        ),
        CandidateSlot(
            Angle("C", "自然", ""), None, status=SlotStatus.FAILED, detail="高德限流"
        ),
    ]
    return s


# ---------- 空输入不再死循环 ----------


def test_awaiting_with_no_command_returns_the_pending_question():
    """原设计在这里空转死循环。"""
    s = _awaiting_confirm()
    out = advance(s, _deps(), None)
    assert isinstance(out, NeedInput)
    assert out.kind is InputKind.CONFIRM_REQUIREMENTS
    assert out.revision == 3


def test_awaiting_with_no_command_does_not_mutate_or_bump():
    s = _awaiting_confirm()
    before = dumps(s)
    advance(s, _deps(), None)
    assert dumps(s) == before


def test_pending_payload_for_choice_stage_is_the_candidate_list():
    s = _awaiting_choice()
    out = advance(s, _deps(), None)
    assert out.kind is InputKind.CHOOSE_OR_FEEDBACK
    assert [c.angle.key for c in out.payload] == ["A", "B", "C"]


def test_pending_is_repeatable_and_side_effect_free():
    s = _awaiting_choice()
    first, second = advance(s, _deps(), None), advance(s, _deps(), None)
    assert first == second
    assert s.revision == 5


# ---------- 陈旧 revision ----------


def test_stale_revision_is_rejected():
    s = _awaiting_confirm()
    out = advance(s, _deps(), ConfirmRequirements(expected_revision=2))
    assert isinstance(out, Rejected)
    assert out.reason is RejectReason.STALE_REVISION


def test_rejected_carries_the_current_question():
    s = _awaiting_confirm()
    out = advance(s, _deps(), ConfirmRequirements(expected_revision=2))
    assert out.current.kind is InputKind.CONFIRM_REQUIREMENTS
    assert out.current.revision == 3


# ---------- 阶段不匹配不再被静默吞掉 ----------


def test_wrong_command_for_stage_is_rejected_not_ignored():
    s = _awaiting_confirm()
    out = advance(s, _deps(), ChooseCandidate(3, "A"))
    assert isinstance(out, Rejected)
    assert out.reason is RejectReason.WRONG_COMMAND_FOR_STAGE


def test_command_while_in_a_work_stage_is_rejected():
    s = TripState.new("x", run_id="r1")
    s.stage = Stage.COLLECT
    out = advance(s, _deps(), ConfirmRequirements(0))
    assert isinstance(out, Rejected)
    assert out.reason is RejectReason.WRONG_COMMAND_FOR_STAGE


# ---------- 必答项 ----------


def test_confirm_is_rejected_when_required_fields_missing():
    s = _awaiting_confirm(reqs=Requirements())
    out = advance(s, _deps(), ConfirmRequirements(3))
    assert out.reason is RejectReason.MISSING_REQUIRED


def test_missing_required_does_not_fall_back_to_collect():
    """回退 COLLECT 会死循环：输入没变，抽取结果也不会变。"""
    s = _awaiting_confirm(reqs=Requirements())
    advance(s, _deps(), ConfirmRequirements(3))
    assert s.stage is Stage.AWAIT_REQ_CONFIRM


def test_amend_is_allowed_even_when_required_missing():
    """只有用户补充了新信息才值得重跑 COLLECT。

    AmendRequirements 在 AWAIT_REQ_CONFIRM 会把 stage 推回 COLLECT，advance
    在同一次调用里就会跑真正的 collect() —— 模块级 `_deps()` 的
    `client=None` 只够跑本文件其余那些从不触达 _apply/_run_to_pause 的
    拒绝路径，这里需要一个能实际应答一次 collect 的 FakeLlm。
    """
    s = _awaiting_confirm(reqs=Requirements())
    collected = {
        "destination": {"value": "京都", "origin": "USER", "rationale": ""},
        "dates": {"value": None, "origin": None, "rationale": ""},
        "party": {"value": None, "origin": None, "rationale": ""},
    }
    llm = FakeLlm(
        by_role={
            Role.CLASSIFIER: [
                LlmResponse(
                    "end_turn",
                    json.dumps(collected, ensure_ascii=False),
                    [],
                    Usage(10, 10),
                )
            ]
        }
    )
    deps = Deps(client=llm, provider=FakeProvider())
    out = advance(s, deps, AmendRequirements(3, "目的地是京都"))
    assert not isinstance(out, Rejected)


# ---------- 候选 key ----------


def test_unknown_candidate_key_is_rejected():
    s = _awaiting_choice()
    out = advance(s, _deps(), ChooseCandidate(5, "ZZZ"))
    assert out.reason is RejectReason.UNKNOWN_CANDIDATE


def test_candidate_without_itinerary_is_unselectable():
    s = _awaiting_choice()
    out = advance(s, _deps(), ChooseCandidate(5, "C"))
    assert out.reason is RejectReason.UNSELECTABLE_CANDIDATE


def test_exhausted_candidate_is_selectable():
    """带着遗留硬伤定稿是用户的权利——问题都摆在他面前了。"""
    s = _awaiting_choice()
    out = advance(s, _deps(), ChooseCandidate(5, "B"))
    assert not isinstance(out, Rejected)


def test_feedback_on_unknown_candidate_is_rejected():
    s = _awaiting_choice()
    out = advance(s, _deps(), GiveFeedback(5, "ZZZ", "太赶了"))
    assert out.reason is RejectReason.UNKNOWN_CANDIDATE


def test_feedback_on_failed_candidate_is_rejected():
    s = _awaiting_choice()
    out = advance(s, _deps(), GiveFeedback(5, "C", "太赶了"))
    assert out.reason is RejectReason.UNSELECTABLE_CANDIDATE


# ---------- 核心不变量 ----------

_REJECTING_COMMANDS = [
    ("stale", ConfirmRequirements(999)),
    ("wrong_stage", ChooseCandidate(3, "A")),
    ("unknown_key", GiveFeedback(3, "ZZZ", "x")),
]


@pytest.mark.parametrize("label,cmd", _REJECTING_COMMANDS)
def test_rejected_never_mutates_state(label, cmd):
    s = _awaiting_confirm()
    before, rev = dumps(s), s.revision
    out = advance(s, _deps(), cmd)
    assert isinstance(out, Rejected), label
    assert dumps(s) == before, label
    assert s.revision == rev, label


def test_awaiting_stages_are_all_covered_by_allowed_commands():
    from tripplan.state import ALLOWED_COMMANDS

    assert set(ALLOWED_COMMANDS) == set(AWAITING)


# ---------- 终态幂等 ----------


def _done_state() -> TripState:
    s = _awaiting_choice()
    s.stage = Stage.DONE
    s.chosen_key = "A"
    return s


def test_done_state_returns_the_itinerary_without_a_command():
    """重复 resume 一个已定稿的行程，只回同一个答案。"""
    s = _done_state()
    out = advance(s, _deps(), None)
    assert isinstance(out, Done)
    assert out.itinerary.angle.key == "A"


def test_done_state_does_not_bump_revision():
    """否则每次 resume 都无意义地改版本，还会让别人的 expected_revision 失效。"""
    s = _done_state()
    before, snapshot = s.revision, dumps(s)
    advance(s, _deps(), None)
    advance(s, _deps(), None)
    assert s.revision == before
    assert dumps(s) == snapshot


def test_done_state_is_idempotent_across_repeated_calls():
    s = _done_state()
    assert advance(s, _deps(), None) == advance(s, _deps(), None)


def test_command_replayed_against_done_returns_done_not_rejected():
    """DONE 没有待回答的问题，构造 Rejected(..., _pending(state)) 会伪造一个
    根本不存在的 NeedInput。"""
    s = _done_state()
    out = advance(s, _deps(), ChooseCandidate(s.revision, "B"))
    assert isinstance(out, Done)
    assert out.itinerary.angle.key == "A"  # 不会改选
    assert s.chosen_key == "A"


# ---------- 最终评审裁定 2：非等待态不许伪造一个问题 ----------


@pytest.mark.parametrize(
    "stage", [Stage.COLLECT, Stage.GENERATE, Stage.REFINE], ids=lambda s: s.value
)
def test_rejecting_in_a_working_stage_carries_no_fabricated_question(stage):
    """_pending 原来的 else 分支对任何非 AWAIT_REQ_CONFIRM 的阶段都返回一个
    CHOOSE_OR_FEEDBACK。工作态下 candidates 是空列表，render_candidates([])
    渲染出来的是「候选全部生成失败，没有可选的方案——请先修改需求后重试。」
    ——对一个从没开始生成过的行程而言这不是"提示为空"，是一句**错误的
    诊断**，而且是用户会照着去改需求的那种。"""
    from tripplan.render.candidates import render_candidates

    s = TripState.new("去京都", run_id="r1")
    s.stage, s.revision, s.requirements = stage, 4, _full_reqs()

    out = advance(s, _deps(), ConfirmRequirements(4))

    assert isinstance(out, Rejected)
    assert out.reason is RejectReason.WRONG_COMMAND_FOR_STAGE
    assert out.current is None
    # 状态与 revision 都不许动（拒绝分支的老不变量，顺带守住）
    assert s.stage is stage and s.revision == 4
    # 反证那句错误诊断确实存在：拿同一批（空）候选去渲染就能看见它。
    assert "候选全部生成失败" in render_candidates(list(s.candidates))


@pytest.mark.parametrize("stage", list(AWAITING), ids=lambda s: s.value)
def test_rejecting_in_an_awaiting_stage_still_carries_the_real_question(stage):
    """反证：真正在等人的两个阶段照旧带着能重新渲染的 NeedInput。"""
    s = _awaiting_confirm() if stage is Stage.AWAIT_REQ_CONFIRM else _awaiting_choice()
    out = advance(s, _deps(), ConfirmRequirements(s.revision - 1))  # 陈旧 revision

    assert isinstance(out, Rejected)
    assert out.reason is RejectReason.STALE_REVISION
    assert out.current is not None
    assert out.current.revision == s.revision
