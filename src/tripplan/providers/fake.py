"""确定性假数据。让全部离线测试可以覆盖 resolver 与编排层。"""

from datetime import datetime, timedelta, timezone

from tripplan.models.common import LatLng, TravelMode
from tripplan.models.facts import PoiFact
from tripplan.providers.base import ProviderError, RouteObservation

_FETCHED_AT = datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=9)))

# 1x1 透明 PNG，够 HTML 渲染测试用且不触网
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

_TIMEZONES = {
    "京都": "Asia/Tokyo",
    "东京": "Asia/Tokyo",
    "大阪": "Asia/Tokyo",
    "上海": "Asia/Shanghai",
    "北京": "Asia/Shanghai",
    "巴黎": "Europe/Paris",
    "伦敦": "Europe/London",
}


def _key(p: LatLng) -> tuple[float, float]:
    return (round(p.lat, 6), round(p.lng, 6))


def _normalize_coord_pair(pair: tuple[float, float]) -> tuple[float, float]:
    """Normalize a coordinate pair to 6 decimal places."""
    return (round(pair[0], 6), round(pair[1], 6))


class FakeProvider:
    def __init__(
        self,
        pois=None,
        fail_pois=None,
        routes=None,
        fail_routes=None,
        timezones=None,
        opening_hours=None,
    ):
        self._pois = pois or {}
        # search_poi() already matches _pois by query alone (city is not part
        # of the key there), so fail_pois mirrors that: a plain set of query
        # strings. Unlike coordinate pairs, strings have no float-precision
        # mismatch to normalize away — membership is exact by construction.
        self._fail_pois = set(fail_pois or set())
        # Normalize route pairs to 6 decimal precision at construction time
        # so that lookups with high-precision coordinates still match
        self._routes = {
            (_normalize_coord_pair(origin), _normalize_coord_pair(dest)): duration
            for (origin, dest), duration in (routes or {}).items()
        }
        # Normalize fail_routes set similarly
        self._fail_routes = {
            (_normalize_coord_pair(origin), _normalize_coord_pair(dest))
            for origin, dest in (fail_routes or set())
        }
        self._timezones = {**_TIMEZONES, **(timezones or {})}
        self._opening_hours = opening_hours or {}
        self.call_log: list[str] = []

    def search_poi(self, query: str, city: str) -> list[PoiFact]:
        self.call_log.append("search_poi")
        if query in self._fail_pois:
            raise ProviderError(f"POI 查询失败：{query}")
        return [
            PoiFact(
                id=pid,
                name=query,
                coords=LatLng(lat, lng),
                opening_hours=self._opening_hours.get(pid),
                ticket=None,
                source="fake",
                fetched_at=_FETCHED_AT,
            )
            for pid, lat, lng in self._pois.get(query, [])
        ]

    def route(self, origin, dest, mode, depart_at) -> RouteObservation:
        self.call_log.append("route")
        pair = (_key(origin), _key(dest))
        if pair in self._fail_routes:
            raise ProviderError(f"路线查询失败：{pair}")
        minutes = self._routes.get(pair)
        if minutes is None:
            # 确定性兜底：按坐标差算一个稳定值
            delta = abs(origin.lat - dest.lat) + abs(origin.lng - dest.lng)
            minutes = max(5, int(delta * 600))
        return RouteObservation(
            mode=mode,
            duration_min=minutes,
            distance_m=minutes * 800,
            polyline=f"fake:{pair[0][0]},{pair[0][1]}->{pair[1][0]},{pair[1][1]}",
            source="fake",
            fetched_at=_FETCHED_AT,
        )

    def static_map(self, points, polyline=None) -> bytes:
        self.call_log.append("static_map")
        return _PNG

    def timezone_of(self, city: str) -> str:
        self.call_log.append("timezone_of")
        tz = self._timezones.get(city)
        if tz is None:
            raise ProviderError(f"查不到城市时区：{city}")
        return tz
