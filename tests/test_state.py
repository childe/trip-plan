import pytest

from tripplan.models.itinerary import Angle, Itinerary
from tripplan.state import (
    ALLOWED_COMMANDS,
    AWAITING,
    AmendRequirements,
    CandidateSlot,
    ChooseCandidate,
    ConfirmRequirements,
    GiveFeedback,
    SlotStatus,
    Stage,
    TripState,
)


def _angle(key: str) -> Angle:
    return Angle(key=key, title=f"方案{key}", description="")


def _slot(key: str, has_itin: bool = True) -> CandidateSlot:
    itin = Itinerary(angle=_angle(key)) if has_itin else None
    return CandidateSlot(angle=_angle(key), itinerary=itin, status=SlotStatus.OK)


def test_new_state_starts_at_collect_with_revision_zero():
    s = TripState.new("去京都玩5天", run_id="r1")
    assert s.stage is Stage.COLLECT
    assert s.revision == 0
    assert s.candidates == []
    assert s.chosen_key is None
    assert s.trip_timezone is None


def test_slot_lookup_by_angle_key():
    s = TripState.new("x", run_id="r1")
    s.candidates = [_slot("A"), _slot("B")]
    assert s.slot("B").angle.key == "B"
    assert s.slot("Z") is None


def test_chosen_returns_none_before_selection():
    s = TripState.new("x", run_id="r1")
    s.candidates = [_slot("A")]
    assert s.chosen() is None


def test_chosen_follows_chosen_key():
    s = TripState.new("x", run_id="r1")
    s.candidates = [_slot("A"), _slot("B")]
    s.chosen_key = "B"
    assert s.chosen().angle.key == "B"


def test_awaiting_contains_exactly_the_two_pause_stages():
    assert AWAITING == frozenset({Stage.AWAIT_REQ_CONFIRM, Stage.AWAIT_CHOICE})


def test_awaiting_and_work_stages_are_disjoint():
    """等待态在进入工作循环前就被拦下，两个集合不能相交。"""
    work = {Stage.COLLECT, Stage.GENERATE, Stage.REFINE, Stage.DONE}
    assert AWAITING & work == frozenset()
    assert AWAITING | work == set(Stage)


def test_allowed_commands_cover_every_awaiting_stage():
    assert set(ALLOWED_COMMANDS) == set(AWAITING)


def test_allowed_commands_per_stage():
    assert ALLOWED_COMMANDS[Stage.AWAIT_REQ_CONFIRM] == frozenset(
        {ConfirmRequirements, AmendRequirements}
    )
    assert ALLOWED_COMMANDS[Stage.AWAIT_CHOICE] == frozenset(
        {ChooseCandidate, GiveFeedback, AmendRequirements}
    )


def test_commands_are_frozen():
    cmd = ChooseCandidate(expected_revision=3, angle_key="A")
    with pytest.raises(Exception):
        cmd.angle_key = "B"


def test_every_command_carries_expected_revision():
    for cmd in (
        ConfirmRequirements(1),
        AmendRequirements(1, "更便宜点"),
        ChooseCandidate(1, "A"),
        GiveFeedback(1, "A", "第2天太赶"),
    ):
        assert cmd.expected_revision == 1
