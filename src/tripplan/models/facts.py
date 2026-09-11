"""对行程的外部观测。由 resolver 触网产生，由 validator 纯函数消费。"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from tripplan.models.common import LatLng, Money, TravelMode


@dataclass(frozen=True)
class PoiFact:
    id: str
    name: str
    coords: LatLng
    opening_hours: str | None
    ticket: Money | None
    source: str
    fetched_at: datetime


@dataclass(frozen=True)
class Resolved:
    fact: PoiFact


@dataclass(frozen=True)
class Ambiguous:
    """同名匹配到多个。不擅自挑一个——挑错了下游全部建立在错坐标上。"""

    candidates: list[PoiFact]


@dataclass(frozen=True)
class NotFound:
    query: str


PoiResolution = Resolved | Ambiguous | NotFound


@dataclass(frozen=True)
class RouteFact:
    day_id: str
    from_activity_id: str
    to_activity_id: str
    depart_at: datetime  # 地铁班次与高峰拥堵都取决于出发时刻
    mode: TravelMode
    duration_min: int
    distance_m: int
    polyline: str  # HTML 画路线用
    source: str
    fetched_at: datetime


@dataclass(frozen=True)
class WeatherFact:
    date_iso: str
    summary: str
    temp_c_min: float
    temp_c_max: float
    source: str


class GapKind(Enum):
    AMBIGUOUS_POI = "AMBIGUOUS_POI"
    POI_NOT_FOUND = "POI_NOT_FOUND"
    AMBIGUOUS_CONSTRAINT = "AMBIGUOUS_CONSTRAINT"
    ROUTE_UNAVAILABLE = "ROUTE_UNAVAILABLE"
    WEATHER_UNAVAILABLE = "WEATHER_UNAVAILABLE"


@dataclass(frozen=True)
class Gap:
    """没查到的事实。显式记录，让规则据此降级而不是静默按 0 处理。"""

    kind: GapKind
    subject: str
    detail: str


@dataclass(frozen=True)
class FactSnapshot:
    poi_by_activity: dict[str, PoiResolution]  # activity.id -> 解析结果
    constraint_pois: dict[str, PoiResolution]  # must_visit/avoid 原文 -> 解析结果
    routes: list[RouteFact]
    weather: dict[str, WeatherFact]  # ISO 日期串 -> 天气
    trip_timezone: str  # 抄自 TripState，供回放核对
    resolved_at: datetime
    gaps: list[Gap]

    def route(self, day_id: str, from_id: str, to_id: str) -> RouteFact | None:
        for r in self.routes:
            if (r.day_id, r.from_activity_id, r.to_activity_id) == (
                day_id,
                from_id,
                to_id,
            ):
                return r
        return None

    def poi_id_for(self, activity_id: str) -> str | None:
        match self.poi_by_activity.get(activity_id):
            case Resolved(fact):
                return fact.id
            case _:
                return None

    def constraint_poi_id(self, query: str) -> str | None:
        match self.constraint_pois.get(query):
            case Resolved(fact):
                return fact.id
            case _:
                return None

    def resolved_poi_ids(self) -> set[str]:
        return {
            r.fact.id for r in self.poi_by_activity.values() if isinstance(r, Resolved)
        }
