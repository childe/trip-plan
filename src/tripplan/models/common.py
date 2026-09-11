"""跨模块共用的基础类型。"""

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum


class Origin(Enum):
    """字段取值的来源。与「是否已确认」正交。"""

    USER = "USER"
    MODEL = "MODEL"


@dataclass(frozen=True)
class Field[T]:
    """带来源与确认状态的字段。

    value is None 表示尚无取值（早先设计里的 MISSING）。
    origin 记录「谁给的值」，confirmed 记录「用户认没认」——两者正交，
    确认操作只动 confirmed，不抹掉 origin。
    """

    value: T | None = None
    origin: Origin | None = None
    confirmed: bool = False
    rationale: str = ""

    def confirm(self) -> "Field[T]":
        return replace(self, confirmed=True)


class Confidence(Enum):
    VERIFIED = "VERIFIED"
    ESTIMATED = "ESTIMATED"


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str
    confidence: Confidence
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("Money.amount 必须是 Decimal，不接受 float")


@dataclass(frozen=True)
class LatLng:
    lat: float
    lng: float


class TravelMode(Enum):
    WALK = "WALK"
    TRANSIT = "TRANSIT"
    DRIVE = "DRIVE"
