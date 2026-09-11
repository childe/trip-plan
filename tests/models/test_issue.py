from tripplan.models.issue import (
    ActivityRef,
    DayRef,
    Issue,
    Severity,
    Source,
    has_blocking,
)


def _issue(sev: Severity) -> Issue:
    return Issue(severity=sev, source=Source.RULE, code="R1", message="x")


def test_has_blocking_true_only_for_blocking():
    assert has_blocking([_issue(Severity.BLOCKING)]) is True
    assert has_blocking([_issue(Severity.WARNING)]) is False
    assert has_blocking([_issue(Severity.SUGGESTION)]) is False
    assert has_blocking([]) is False


def test_has_blocking_scans_whole_list():
    issues = [_issue(Severity.WARNING), _issue(Severity.BLOCKING)]
    assert has_blocking(issues) is True


def test_from_human_is_blocking_and_sourced_human():
    """人提的意见必须驱动一轮修订，因此是 BLOCKING。"""
    i = Issue.from_human("第2天太赶了")
    assert i.severity is Severity.BLOCKING
    assert i.source is Source.HUMAN
    assert "第2天太赶了" in i.message


def test_refs_locate_precisely():
    a = ActivityRef(day_id="d1", activity_id="a3")
    d = DayRef(day_id="d1")
    assert (a.day_id, a.activity_id) == ("d1", "a3")
    assert d.day_id == "d1"
