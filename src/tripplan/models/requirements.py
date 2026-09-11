"""用户需求：每个字段都带来源与确认状态。"""

from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from tripplan.models.common import Field


class Pace(Enum):
    RELAXED = "RELAXED"
    NORMAL = "NORMAL"
    PACKED = "PACKED"


class Basis(Enum):
    PER_PERSON = "PER_PERSON"
    TOTAL = "TOTAL"


class CostKind(Enum):
    FLIGHT = "FLIGHT"
    LODGING = "LODGING"
    TICKET = "TICKET"
    MEAL = "MEAL"
    LOCAL_TRANSIT = "LOCAL_TRANSIT"


@dataclass(frozen=True)
class BudgetSpec:
    amount: Decimal
    currency: str
    basis: Basis
    includes: frozenset[CostKind]

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("BudgetSpec.amount 必须是 Decimal")


@dataclass(frozen=True)
class Party:
    adults: int
    children: int = 0
    seniors: int = 0

    @property
    def total(self) -> int:
        return self.adults + self.children + self.seniors


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    @property
    def days(self) -> int:
        """含首尾的天数。10-01 到 10-05 是 5 天。"""
        return (self.end - self.start).days + 1


@dataclass(frozen=True)
class Transfer:
    """抵达或离开。跨境行程两端不在同一时区，必须存带 offset 的时刻。"""

    at: datetime
    mode: str

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("Transfer.at 必须是 tz-aware datetime")


@dataclass
class Requirements:
    destination: Field[str] = field(default_factory=Field)
    dates: Field[DateRange] = field(default_factory=Field)
    party: Field[Party] = field(default_factory=Field)
    arrival: Field[Transfer] = field(default_factory=Field)
    departure: Field[Transfer] = field(default_factory=Field)
    budget: Field[BudgetSpec] = field(default_factory=Field)
    styles: Field[list[str]] = field(default_factory=Field)
    pace: Field[Pace] = field(default_factory=Field)
    must_visit: Field[list[str]] = field(default_factory=Field)
    avoid: Field[list[str]] = field(default_factory=Field)
    lodging_area: Field[str] = field(default_factory=Field)
    constraints: Field[list[str]] = field(default_factory=Field)


#: 这三项无法可靠推断——虚构它们会让整个规划建立在假约束上，
#: 而下游的确定性校验还会拿这份虚构去判 BLOCKING，比不校验更糟。
REQUIRED = ("destination", "dates", "party")


def missing_required(reqs: Requirements) -> list[str]:
    return [name for name in REQUIRED if getattr(reqs, name).value is None]


def describe_value(value) -> str:
    """把字段值渲染成人话。agents 与 render 共用，避免两处各写一份。"""
    if isinstance(value, DateRange):
        return f"{value.start} 至 {value.end}（{value.days} 天）"
    if isinstance(value, Party):
        return f"成人 {value.adults} 儿童 {value.children} " f"老人 {value.seniors}"
    if isinstance(value, BudgetSpec):
        kinds = "、".join(sorted(k.value for k in value.includes))
        return f"{value.amount} {value.currency}" f"（{value.basis.value}，含 {kinds}）"
    if isinstance(value, Transfer):
        return f"{value.at.isoformat()}（{value.mode}）"
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return "、".join(str(v) for v in value)
    return str(value)


def mark_all_confirmed(reqs: Requirements) -> Requirements:
    """把所有已有取值的字段标为已确认，不改动 origin。"""
    updates = {}
    for f in fields(reqs):
        current: Field = getattr(reqs, f.name)
        if current.value is not None:
            updates[f.name] = current.confirm()
    return replace(reqs, **updates)
