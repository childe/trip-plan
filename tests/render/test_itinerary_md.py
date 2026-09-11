from datetime import date
from decimal import Decimal

from tripplan.models.common import Confidence, Field, Money, Origin
from tripplan.models.facts import Gap, GapKind
from tripplan.models.issue import Issue, Severity, Source
from tripplan.models.itinerary import Category
from tripplan.models.requirements import (
    Basis,
    BudgetSpec,
    CostKind,
    Requirements,
)
from tripplan.render.itinerary_md import render_itinerary_md

D1 = date(2026, 10, 1)


def _itin(mk):
    return mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺"),
                    mk.act(
                        "d1a2",
                        "d1",
                        "12:00",
                        "13:00",
                        query="某食堂",
                        category=Category.MEAL,
                        cost=Money(Decimal("1500"), "JPY", Confidence.ESTIMATED, "llm"),
                    ),
                ],
            )
        ]
    )


def test_renders_days_and_activities_in_order(mk):
    out = render_itinerary_md(_itin(mk), mk.facts(), mk.reqs())
    assert out.index("清水寺") < out.index("某食堂")
    assert "09:00" in out and "2026-10-01" in out


def test_inserts_transit_legs_between_activities(mk):
    facts = mk.facts(routes=[mk.route("d1", "d1a1", "d1a2", 40)])
    out = render_itinerary_md(_itin(mk), facts, mk.reqs())
    assert "40" in out
    assert out.index("清水寺") < out.index("40") < out.index("某食堂")


def test_marks_unverified_transit_instead_of_leaving_a_blank(mk):
    facts = mk.facts(gaps=[mk.gap(GapKind.ROUTE_UNAVAILABLE, "d1a1->d1a2", "高德限流")])
    out = render_itinerary_md(_itin(mk), facts, mk.reqs())
    assert "未能核实" in out


def test_shows_layered_budget_ledger(mk):
    reqs = mk.reqs(
        budget=Field(
            BudgetSpec(
                Decimal("15000"), "JPY", Basis.TOTAL, frozenset({CostKind.MEAL})
            ),
            Origin.USER,
        )
    )
    out = render_itinerary_md(_itin(mk), mk.facts(), reqs)
    assert "估算" in out
    assert "未知" in out  # 清水寺 cost 为 None
    assert "1500" in out


def test_estimated_amounts_are_visually_distinct_from_verified(mk):
    reqs = mk.reqs(
        budget=Field(
            BudgetSpec(
                Decimal("15000"), "JPY", Basis.TOTAL, frozenset({CostKind.MEAL})
            ),
            Origin.USER,
        )
    )
    out = render_itinerary_md(_itin(mk), mk.facts(), reqs)
    assert "已核实" in out and "估算" in out


def test_lists_outstanding_issues(mk):
    itin = _itin(mk)
    itin.issues = [Issue(Severity.WARNING, Source.RULE, "R8", "没安排晚餐")]
    out = render_itinerary_md(itin, mk.facts(), mk.reqs())
    assert "没安排晚餐" in out


def test_lists_unverified_facts_honestly(mk):
    facts = mk.facts(gaps=[mk.gap(GapKind.POI_NOT_FOUND, "d1a1", "查不到")])
    out = render_itinerary_md(_itin(mk), facts, mk.reqs())
    assert "未核实" in out or "未能核实" in out


def test_renderer_does_not_touch_the_network(mk):
    """同一份 state.json 反复渲染必须得到同样的结果。"""
    import ast
    import pathlib

    import tripplan.render.itinerary_md as mod

    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    names = {
        a.name
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Import)
        for a in n.names
    }
    names |= {
        n.module or ""
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.ImportFrom)
    }
    assert not (names & {"httpx", "requests", "urllib"})
    assert not any(m.startswith("tripplan.providers") for m in names)


def test_output_is_stable_across_repeated_renders(mk):
    args = (_itin(mk), mk.facts(), mk.reqs())
    assert render_itinerary_md(*args) == render_itinerary_md(*args)
