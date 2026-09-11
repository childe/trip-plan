from datetime import date
from decimal import Decimal

from tripplan.models.common import Field, Origin
from tripplan.models.requirements import (
    Basis,
    BudgetSpec,
    CostKind,
    DateRange,
    Pace,
    Party,
    Requirements,
)
from tripplan.render.requirement_card import render_requirement_card


def _reqs(**kw):
    base = dict(
        destination=Field("京都", Origin.USER),
        dates=Field(DateRange(date(2026, 10, 1), date(2026, 10, 5)), Origin.USER),
        party=Field(Party(adults=2), Origin.USER),
    )
    base.update(kw)
    return Requirements(**base)


def test_shows_user_stated_fields_plainly():
    out = render_requirement_card(_reqs())
    assert "京都" in out
    assert "2026-10-01" in out


def test_marks_inferred_fields_so_the_user_knows_where_to_look():
    """用户一眼知道该盯哪几行，不必通读全表。"""
    out = render_requirement_card(
        _reqs(pace=Field(Pace.RELAXED, Origin.MODEL, rationale="带老人"))
    )
    line = next(l for l in out.splitlines() if "pace" in l or "节奏" in l)
    assert "?" in line
    assert "带老人" in line


def test_does_not_mark_user_stated_fields():
    out = render_requirement_card(_reqs(pace=Field(Pace.RELAXED, Origin.USER)))
    line = next(l for l in out.splitlines() if "节奏" in l)
    assert "?" not in line


def test_lists_missing_required_fields_prominently():
    out = render_requirement_card(Requirements())
    assert "缺少" in out or "待补充" in out
    assert "目的地" in out


def test_omits_empty_optional_fields():
    out = render_requirement_card(_reqs())
    assert "住宿区域" not in out


def test_renders_budget_with_currency_basis_and_inclusions():
    out = render_requirement_card(
        _reqs(
            budget=Field(
                BudgetSpec(
                    Decimal("15000"),
                    "CNY",
                    Basis.TOTAL,
                    frozenset({CostKind.TICKET, CostKind.MEAL}),
                ),
                Origin.USER,
            )
        )
    )
    assert "15000" in out and "CNY" in out
    assert "TICKET" in out or "门票" in out


def test_confirmed_inferred_field_still_shows_its_origin():
    """确认不抹掉「这值本来是猜的」。"""
    out = render_requirement_card(
        _reqs(
            pace=Field(Pace.RELAXED, Origin.MODEL, confirmed=True, rationale="带老人")
        )
    )
    assert "带老人" in out
