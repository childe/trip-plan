"""分层账单：把「已核实 / 估算 / 未知」分开呈现，而不是给一个假精确的合计。"""

from dataclasses import dataclass
from decimal import Decimal

from tripplan.models.common import Confidence
from tripplan.models.itinerary import Itinerary
from tripplan.models.requirements import Basis, Requirements


@dataclass(frozen=True)
class BudgetLedger:
    currency: str
    verified: Decimal
    verified_count: int
    estimated: Decimal
    estimated_count: int
    unknown_count: int
    currency_mismatch: bool
    budget_limit: Decimal | None

    @property
    def total(self) -> Decimal:
        return self.verified + self.estimated

    @property
    def complete(self) -> bool:
        """没有任何未知项。"""
        return self.unknown_count == 0

    @property
    def all_verified(self) -> bool:
        """已知项全部经过核实。"""
        return self.estimated_count == 0

    @property
    def over_budget(self) -> bool:
        return self.budget_limit is not None and self.total > self.budget_limit


def build_ledger(itin: Itinerary, reqs: Requirements) -> BudgetLedger:
    spec = reqs.budget.value

    # 如果有预算，用其币种；否则从行程第一个有价格的活动推断。
    if spec is not None:
        currency = spec.currency
    else:
        # 从第一个有成本的活动推断币种；全无价格则用 CNY。
        currency = "CNY"
        for act in itin.all_activities():
            if act.cost is not None:
                currency = act.cost.currency
                break

    verified = estimated = Decimal("0")
    v_count = e_count = unknown = 0
    mismatch = False

    for act in itin.all_activities():
        cost = act.cost
        if cost is None:
            unknown += 1  # None 是「未知」，不是免费
            continue
        if cost.currency != currency:
            mismatch = True
            unknown += 1  # 币种对不上，不硬加
            continue
        if cost.confidence is Confidence.VERIFIED:
            verified += cost.amount
            v_count += 1
        else:
            estimated += cost.amount
            e_count += 1

    limit: Decimal | None = None
    if spec is not None:
        party = reqs.party.value
        head = party.total if (party and spec.basis is Basis.PER_PERSON) else 1
        limit = spec.amount * head

    return BudgetLedger(
        currency=currency,
        verified=verified,
        verified_count=v_count,
        estimated=estimated,
        estimated_count=e_count,
        unknown_count=unknown,
        currency_mismatch=mismatch,
        budget_limit=limit,
    )
