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


def test_error_placeholder_does_not_block_other_candidates_from_rendering():
    """orchestrator.py 在角度生成整体失败时构造的字面量占位 slot。

    render_candidates 把所有 slot 拼进同一个字符串，没有逐个隔离——
    itinerary is None 分支必须先短路掉，不能让 slot.itinerary.days
    有机会被访问，否则一个占位候选会带崩整份候选列表的渲染，用户
    什么都看不到，即便另一个候选其实成功了。
    """
    healthy = _slot("A")
    placeholder = CandidateSlot(
        Angle("_error", "角度生成失败", ""),
        None,
        None,
        SlotStatus.FAILED,
        "角度生成失败：LLM 超时",
    )
    out = render_candidates([healthy, placeholder])
    assert "方案A" in out
    assert "角度生成失败：LLM 超时" in out
    assert "无法选择" in out or "不可选" in out


def test_all_candidates_failed_prompts_amendment_not_an_empty_choice():
    """全部候选都失败时只剩这一个占位 slot——收尾不能诱导「选一份（）」，
    要引导去改需求。"""
    placeholder = CandidateSlot(
        Angle("_error", "角度生成失败", ""),
        None,
        None,
        SlotStatus.FAILED,
        "角度生成失败：LLM 超时",
    )
    out = render_candidates([placeholder])
    assert "选一份（）" not in out
    assert "改" in out or "需求" in out


def test_failed_slot_that_still_has_an_itinerary_shows_why_it_failed():
    """revise/critic 轮挂掉时 slot.py:86-89 会保住已生成的 itin，于是
    itinerary 非空、status=FAILED——当前实现（candidates.py:23 只认
    EXHAUSTED）下 detail 完全不显示，而 candidates.py:33 照常把它列进
    可选项。用户会选中一份中途挂掉的行程而毫不知情。

    注意与已有的 test_failed_candidate_is_shown_but_marked_unselectable
    的区别：那条用的是 has_itin=False，走的是 candidates.py:15 那个分支。
    """
    out = render_candidates(
        [
            _slot(
                "D",
                SlotStatus.FAILED,
                detail="外部依赖失败：凭据被拒绝（401）",
                has_itin=True,
            )
        ]
    )
    assert "401" in out
