"""外部数据访问的契约。实现者：FakeProvider（测试）、AmapProvider（生产）。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from tripplan.models.common import LatLng, TravelMode
from tripplan.models.facts import PoiFact


class ProviderError(Exception):
    """外部依赖失败：网络错误、限流、鉴权失败、查无此城——不管是高德这类地理
    服务，还是 LLM 本身的传输层故障（连接失败 / 429 / 5xx），都算在内。
    名字虽然是 Provider，覆盖的是"任何外部依赖"，不止地理服务一家。

    由 run_slot 兜住（Task 18），转成 SlotStatus.FAILED 而不是炸穿整组候选。
    """


@dataclass(frozen=True)
class RouteObservation:
    """provider 的原始返回。resolver 给它补上 day_id 与活动 ID 后成为 RouteFact。"""

    mode: TravelMode
    duration_min: int
    distance_m: int
    polyline: str
    source: str
    fetched_at: datetime


class GeoProvider(Protocol):
    def search_poi(self, query: str, city: str) -> list[PoiFact]: ...

    def route(
        self, origin: LatLng, dest: LatLng, mode: TravelMode, depart_at: datetime
    ) -> RouteObservation: ...

    def static_map(self, points: list[LatLng], polyline: str | None) -> bytes: ...

    def timezone_of(self, city: str) -> str: ...
