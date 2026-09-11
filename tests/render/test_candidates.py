from tripplan.models.issue import Issue, Severity, Source
from tripplan.models.itinerary import Angle, Itinerary
from tripplan.render.candidates import render_candidates
from tripplan.state import CandidateSlot, SlotStatus


def _slot(key, status=SlotStatus.OK, issues=(), detail="", has_itin=True):
    itin = (
        Itinerary(angle=Angle(key, f"方案{key}", f"{key}的思路"), issues=list(issues))
        if has_itin
        else None
    )
    return CandidateSlot(
        Angle(key, f"方案{key}", f"{key}的思路"), itin, None, status, detail
    )


def test_lists_every_candidate_with_its_angle():
    out = render_candidates([_slot("A"), _slot("B")])
    assert "方案A" in out and "A的思路" in out
    assert "方案B" in out


def test_shows_unresolved_issues_as_selection_evidence():
    issues = [Issue(Severity.BLOCKING, Source.RULE, "R2", "第2天通勤3小时")]
    out = render_candidates([_slot("A", issues=issues)])
    assert "第2天通勤3小时" in out


def test_marks_exhausted_candidates_with_their_reason():
    out = render_candidates(
        [_slot("B", SlotStatus.EXHAUSTED, detail="修订3轮后仍有2个硬伤")]
    )
    assert "修订3轮后仍有2个硬伤" in out


def test_failed_candidate_is_shown_but_marked_unselectable():
    """「方案C 因为高德限流没跑完」也是用户有权知道的事实。"""
    out = render_candidates(
        [_slot("C", SlotStatus.FAILED, detail="高德限流", has_itin=False)]
    )
    assert "高德限流" in out
    assert "无法选择" in out or "不可选" in out


def test_shows_how_to_respond():
    out = render_candidates([_slot("A")])
    assert "A" in out
    assert "选" in out
