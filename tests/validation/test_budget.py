from datetime import date, time
from decimal import Decimal

from tripplan.models.common import Confidence, Field, Money, Origin
from tripplan.models.itinerary import Activity, Angle, Category, Day, Itinerary
from tripplan.models.requirements import (
    Basis,
    BudgetSpec,
    CostKind,
    Party,
    Requirements,
)
from tripplan.validation.budget import build_ledger


def _money(v: str, conf: Confidence, cur: str = "CNY") -> Money:
    return Money(Decimal(v), cur, conf, "test")


def _itin(costs: list[Money | None]) -> Itinerary:
    acts = [
        Activity(
            id=f"d1a{i}",
            day_id="d1",
            poi_query=f"p{i}",
            start=time(9),
            end=time(10),
            category=Category.SIGHT,
            cost=c,
            indoor=False,
            note="",
        )
        for i, c in enumerate(costs, start=1)
    ]
    return Itinerary(
        angle=Angle("A", "", ""),
        days=[Day(id="d1", date=date(2026, 10, 1), activities=acts)],
    )


def _reqs(
    amount: str | None = "15000", basis: Basis = Basis.TOTAL, currency: str = "CNY"
) -> Requirements:
    budget = (
        Field(
            BudgetSpec(Decimal(amount), currency, basis, frozenset({CostKind.TICKET})),
            Origin.USER,
        )
        if amount
        else Field()
    )
    return Requirements(party=Field(Party(adults=2), Origin.USER), budget=budget)


def test_splits_verified_estimated_and_unknown():
    led = build_ledger(
        _itin(
            [
                _money("100", Confidence.VERIFIED),
                _money("200", Confidence.ESTIMATED),
                None,
                None,
            ]
        ),
        _reqs(),
    )
    assert led.verified == Decimal("100") and led.verified_count == 1
    assert led.estimated == Decimal("200") and led.estimated_count == 1
    assert led.unknown_count == 2
    assert led.total == Decimal("300")


def test_not_complete_when_any_cost_unknown():
    led = build_ledger(_itin([_money("100", Confidence.VERIFIED), None]), _reqs())
    assert led.complete is False
    assert led.all_verified is True  # 已知的那些确实都核实过


def test_complete_and_all_verified_only_when_no_gaps():
    led = build_ledger(_itin([_money("100", Confidence.VERIFIED)]), _reqs())
    assert led.complete is True
    assert led.all_verified is True


def test_estimated_items_break_all_verified():
    led = build_ledger(_itin([_money("100", Confidence.ESTIMATED)]), _reqs())
    assert led.all_verified is False


def test_per_person_budget_multiplies_by_party_size():
    led = build_ledger(
        _itin([_money("100", Confidence.VERIFIED)]), _reqs("3000", Basis.PER_PERSON)
    )
    assert led.budget_limit == Decimal("6000")  # 3000 × 2 人


def test_total_basis_uses_amount_directly():
    led = build_ledger(_itin([]), _reqs("15000", Basis.TOTAL))
    assert led.budget_limit == Decimal("15000")


def test_over_budget_compares_total_against_limit():
    led = build_ledger(_itin([_money("20000", Confidence.VERIFIED)]), _reqs())
    assert led.over_budget is True


def test_no_budget_means_never_over():
    led = build_ledger(
        _itin([_money("99999", Confidence.VERIFIED)]), _reqs(amount=None)
    )
    assert led.budget_limit is None
    assert led.over_budget is False


def test_mismatched_currency_is_flagged_not_silently_summed():
    """日元花费配人民币预算，不能直接相加。"""
    led = build_ledger(
        _itin(
            [
                _money("100", Confidence.VERIFIED, "CNY"),
                _money("4000", Confidence.ESTIMATED, "JPY"),
            ]
        ),
        _reqs(currency="CNY"),
    )
    assert led.currency_mismatch is True
    assert led.total == Decimal("100")  # 只累加同币种的
    assert led.unknown_count == 1  # 异币种计入未知
