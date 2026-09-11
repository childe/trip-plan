from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from tripplan.models.common import Field, Origin
from tripplan.models.requirements import (
    REQUIRED,
    Basis,
    BudgetSpec,
    CostKind,
    DateRange,
    Pace,
    Party,
    Requirements,
    Transfer,
    mark_all_confirmed,
    missing_required,
)


def _reqs(**kw) -> Requirements:
    base = dict(
        destination=Field(value="京都", origin=Origin.USER),
        dates=Field(
            value=DateRange(date(2026, 10, 1), date(2026, 10, 5)), origin=Origin.USER
        ),
        party=Field(value=Party(adults=2), origin=Origin.USER),
    )
    base.update(kw)
    return Requirements(**base)


def test_date_range_days_is_inclusive():
    assert DateRange(date(2026, 10, 1), date(2026, 10, 5)).days == 5


def test_party_total_counts_everyone():
    assert Party(adults=2, children=1, seniors=1).total == 4


def test_missing_required_lists_empty_fields():
    reqs = Requirements()
    assert missing_required(reqs) == list(REQUIRED)


def test_missing_required_is_empty_when_all_present():
    assert missing_required(_reqs()) == []


def test_missing_required_ignores_optional_fields():
    """budget 缺失不阻塞规划，只有 REQUIRED 三项会。"""
    reqs = _reqs()
    assert reqs.budget.value is None
    assert missing_required(reqs) == []


def test_mark_all_confirmed_preserves_origin():
    reqs = _reqs(
        pace=Field(value=Pace.RELAXED, origin=Origin.MODEL, rationale="带老人")
    )
    out = mark_all_confirmed(reqs)
    assert out.pace.confirmed is True
    assert out.pace.origin is Origin.MODEL  # ★ 推断来源保留
    assert out.destination.confirmed is True


def test_mark_all_confirmed_skips_empty_fields():
    """没有取值的字段不该被标成「已确认」。"""
    out = mark_all_confirmed(Requirements())
    assert out.destination.confirmed is False


def test_budget_spec_holds_currency_basis_and_inclusions():
    b = BudgetSpec(
        amount=Decimal("15000"),
        currency="CNY",
        basis=Basis.TOTAL,
        includes=frozenset({CostKind.TICKET, CostKind.MEAL}),
    )
    assert b.basis is Basis.TOTAL
    assert CostKind.FLIGHT not in b.includes


def test_transfer_requires_tz_aware_datetime():
    import pytest

    with pytest.raises(ValueError):
        Transfer(at=datetime(2026, 10, 1, 9, 0), mode="flight")
    ok = Transfer(
        at=datetime(2026, 10, 1, 9, 0, tzinfo=timezone(timedelta(hours=9))),
        mode="flight",
    )
    assert ok.at.tzinfo is not None
