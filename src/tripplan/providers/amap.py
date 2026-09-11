"""高德实现。v1 唯一的真实数据源。

缓存键包含出发**小时**——同一对坐标在早高峰与午间的耗时不同，
但把分钟也纳入键会让缓存基本失效，小时是合适的粒度。
"""

import base64
from datetime import datetime, timezone

import httpx

from tripplan.models.common import LatLng, TravelMode
from tripplan.models.facts import PoiFact
from tripplan.providers.base import ProviderError, RouteObservation
from tripplan.providers.cache import DiskCache

_BASE = "https://restapi.amap.com/v3"

#: v1 单目的地，城市集合有限，硬编码比多打一次网络请求划算。
_TIMEZONES = {
    "京都": "Asia/Tokyo",
    "东京": "Asia/Tokyo",
    "大阪": "Asia/Tokyo",
    "北海道": "Asia/Tokyo",
    "冲绳": "Asia/Tokyo",
    "首尔": "Asia/Seoul",
    "上海": "Asia/Shanghai",
    "北京": "Asia/Shanghai",
    "成都": "Asia/Shanghai",
    "广州": "Asia/Shanghai",
    "香港": "Asia/Hong_Kong",
    "台北": "Asia/Taipei",
    "曼谷": "Asia/Bangkok",
    "新加坡": "Asia/Singapore",
    "巴黎": "Europe/Paris",
    "伦敦": "Europe/London",
    "罗马": "Europe/Rome",
    "纽约": "America/New_York",
}

_MODE_PATH = {
    TravelMode.TRANSIT: "direction/transit/integrated",
    TravelMode.WALK: "direction/walking",
    TravelMode.DRIVE: "direction/driving",
}


class AmapProvider:
    def __init__(
        self, key: str, cache: DiskCache, http: httpx.Client | None = None
    ) -> None:
        self.key = key
        self.cache = cache
        self.http = http or httpx.Client(timeout=10.0)

    # ---------- 传输 ----------

    def _redact(self, message: str) -> str:
        """兜底：万一以后哪次改动又把 URL/key 带回了消息里，这里再抹一遍。

        key 是每个请求的 query 参数，httpx 异常的 str() 里天然带着完整
        URL——不能让它原样进 ProviderError 的消息，那是用户会看到、会
        截图、会贴进 bug 报告的文本。
        """
        return message.replace(self.key, "***") if self.key else message

    def _get_json(self, path: str, params: dict) -> dict:
        try:
            resp = self.http.get(f"{_BASE}/{path}", params={**params, "key": self.key})
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise ProviderError(
                self._redact(f"高德请求失败：HTTP {e.response.status_code}")
            ) from e
        except httpx.HTTPError as e:
            raise ProviderError(
                self._redact(f"高德请求失败：{type(e).__name__}")
            ) from e
        except ValueError as e:
            # resp.json() 对非 JSON 正文抛的是 json.JSONDecodeError，它是
            # ValueError，既不是 HTTPStatusError 也不是 HTTPError——两个
            # except 都接不住。触发场景很普通：200 + HTML 正文（WAF 拦截页、
            # 门户网络的劫持页、网关限流页）。漏出去之后 _lookup / run_slot
            # 都只认 ProviderError，最终被 _safe_slot 记成「候选线出现未处理
            # 异常」——三条候选同时撞上同一次故障，用户拿到的是三个 FAILED
            # 候选、一份行程都没有，而不是三份带 POI_NOT_FOUND 缺口的行程。
            # 这正是 Task 14 为 KeyError 立下的那条规矩，只是换了个触发点。
            raise ProviderError(self._redact("高德返回的不是合法 JSON")) from e
        if not isinstance(data, dict):
            # 合法 JSON 但不是对象（比如正文就是 `null` / `[]`）：再往下
            # data.get(...) 就是 AttributeError，同样会漏出 ProviderError 之外。
            raise ProviderError(
                self._redact(f"高德返回的 JSON 不是对象：{type(data).__name__}")
            )
        if data.get("status") != "1":
            raise ProviderError(
                self._redact(f"高德返回错误：{data.get('info', '未知')}")
            )
        return data

    def _get_bytes(self, path: str, params: dict) -> bytes:
        try:
            resp = self.http.get(f"{_BASE}/{path}", params={**params, "key": self.key})
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPStatusError as e:
            raise ProviderError(
                self._redact(f"高德请求失败：HTTP {e.response.status_code}")
            ) from e
        except httpx.HTTPError as e:
            raise ProviderError(
                self._redact(f"高德请求失败：{type(e).__name__}")
            ) from e

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    # ---------- GeoProvider ----------

    def search_poi(self, query: str, city: str) -> list[PoiFact]:
        cache_key = f"poi|{city}|{query}"
        raw = self.cache.get(cache_key)
        if raw is None:
            raw = self._get_json(
                "place/text",
                {"keywords": query, "city": city, "citylimit": "true", "offset": "5"},
            )
            self.cache.put(cache_key, raw)

        fetched = self._now()
        out = []
        for p in raw.get("pois", []):
            try:
                lng, lat = (float(x) for x in p["location"].split(","))
                hours = (p.get("business") or {}).get("opentime_week") or None
                out.append(
                    PoiFact(
                        id=p["id"],
                        name=p["name"],
                        coords=LatLng(lat, lng),
                        opening_hours=hours,
                        ticket=None,
                        source="amap:place/text",
                        fetched_at=fetched,
                    )
                )
            except (KeyError, ValueError, TypeError) as e:
                # status == "1" 不代表每个条目字段齐全——不能让 KeyError
                # 逃出去：run_slot 只兜 ProviderError（Task 18），漏网的
                # KeyError 会把整组候选一起炸穿，而不是只失败这一个。
                raise ProviderError(
                    self._redact(f"高德 POI 返回格式异常：{p!r}")
                ) from e
        return out

    def route(
        self, origin: LatLng, dest: LatLng, mode: TravelMode, depart_at: datetime
    ) -> RouteObservation:
        path = _MODE_PATH[mode]
        # 出发小时进键：早高峰与午间耗时不同，但按分钟会让缓存失效
        cache_key = (
            f"route|{mode.value}|{origin.lat:.5f},{origin.lng:.5f}"
            f"|{dest.lat:.5f},{dest.lng:.5f}"
            f"|{depart_at:%Y-%m-%dT%H}"
        )
        raw = self.cache.get(cache_key)
        if raw is None:
            raw = self._get_json(
                path,
                {
                    "origin": f"{origin.lng},{origin.lat}",
                    "destination": f"{dest.lng},{dest.lat}",
                    "city": "",
                    "time": f"{depart_at:%H:%M}",
                    "date": f"{depart_at:%Y-%m-%d}",
                },
            )
            self.cache.put(cache_key, raw)

        transits = (raw.get("route") or {}).get("transits") or []
        if not transits:
            raise ProviderError(
                f"没有可用路线：{origin.lat},{origin.lng} -> " f"{dest.lat},{dest.lng}"
            )
        best = transits[0]
        polyline = _first_polyline(best)
        try:
            duration_min = int(float(best["duration"]) // 60)
            distance_m = int(float(best["distance"]))
        except (KeyError, ValueError, TypeError) as e:
            # 同上：字段缺失或类型不对不能变成 KeyError/ValueError 逃出去，
            # 必须是 ProviderError，让 run_slot 把这一条候选降级而不是整组炸穿。
            raise ProviderError(self._redact(f"高德路线返回格式异常：{best!r}")) from e
        return RouteObservation(
            mode=mode,
            duration_min=duration_min,
            distance_m=distance_m,
            polyline=polyline,
            source=f"amap:{path}",
            fetched_at=self._now(),
        )

    def static_map(self, points: list[LatLng], polyline: str | None = None) -> bytes:
        marker = "|".join(f"{p.lng},{p.lat}" for p in points)
        params = {
            "size": "750*400",
            "scale": "2",
            "markers": f"mid,,A:{marker}" if marker else "",
        }
        if polyline:
            params["paths"] = f"5,0x0000ff,1,,:{polyline}"
        cache_key = "map|" + repr(sorted(params.items()))
        cached = self.cache.get(cache_key)
        if cached is not None:
            return base64.b64decode(cached)
        data = self._get_bytes("staticmap", params)
        self.cache.put(cache_key, base64.b64encode(data).decode("ascii"))
        return data

    def timezone_of(self, city: str) -> str:
        tz = _TIMEZONES.get(city)
        if tz is None:
            raise ProviderError(f"未知城市时区：{city}")
        return tz


def _first_polyline(transit: dict) -> str:
    for seg in transit.get("segments", []):
        buslines = (seg.get("bus") or {}).get("buslines") or []
        for line in buslines:
            if line.get("polyline"):
                return line["polyline"]
        walking = seg.get("walking") or {}
        for step in walking.get("steps", []):
            if step.get("polyline"):
                return step["polyline"]
    return ""
