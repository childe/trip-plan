"""行程。LLM 只产出活动安排；ID 与交通段都由代码补。"""

from dataclasses import dataclass, field, replace
from datetime import date as Date
from datetime import time
from enum import Enum
from typing import Iterator

from tripplan.models.common import Money
from tripplan.models.issue import Issue


class Category(Enum):
    SIGHT = "SIGHT"
    MEAL = "MEAL"
    REST = "REST"
    SHOPPING = "SHOPPING"


@dataclass(frozen=True)
class Angle:
    """一份候选的切入角度。由 LLM 自己想，不写死枚举。"""

    key: str
    title: str
    description: str


@dataclass
class Activity:
    id: str
    day_id: str
    poi_query: str  # LLM 写的名字；解析结果在 FactSnapshot 里
    start: time
    end: time
    category: Category
    cost: Money | None  # None 表示「未知」，不表示免费
    indoor: bool
    note: str


@dataclass
class Day:
    id: str
    date: Date
    activities: list[Activity] = field(default_factory=list)
    lodging: str | None = None


@dataclass
class Itinerary:
    angle: Angle
    days: list[Day] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def all_activities(self) -> Iterator[Activity]:
        for day in self.days:
            yield from day.activities

    def activity(self, activity_id: str) -> Activity | None:
        for a in self.all_activities():
            if a.id == activity_id:
                return a
        return None


def assign_ids(itin: Itinerary) -> Itinerary:
    """按位置分配稳定 ID。幂等。

    ID 由代码分配而非 LLM 产出——让模型自己维护稳定 ID 只是白白增加它出错的
    机会，而按位置分配是确定性的。
    """
    days = []
    for di, day in enumerate(itin.days, start=1):
        day_id = f"d{di}"
        acts = [
            replace(a, id=f"{day_id}a{ai}", day_id=day_id)
            for ai, a in enumerate(day.activities, start=1)
        ]
        days.append(replace(day, id=day_id, activities=acts))
    return replace(itin, days=days)
