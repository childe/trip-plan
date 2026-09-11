"""把行程解析成不可变的事实快照。**本项目唯一的触网点。**

调用方传入 tz —— resolver 不自己解析时区。两条独立的解析路径迟早会不一致，
而且不一致时没有任何地方会报错。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from tripplan.models.common import TravelMode
from tripplan.models.facts import (
    Ambiguous,
    FactSnapshot,
    Gap,
    GapKind,
    NotFound,
    PoiResolution,
    Resolved,
    RouteFact,
)
from tripplan.models.itinerary import Itinerary
from tripplan.models.requirements import Requirements
from tripplan.providers.base import GeoProvider, ProviderError

#: v1 只测算公共交通。多城市（spec §12.4）时会需要按段选择模式。
DEFAULT_MODE = TravelMode.TRANSIT


def resolve_timezone(reqs: Requirements, provider: GeoProvider) -> str:
    """由已确定的 destination 反查 IANA 时区。查不到就退到 UTC 并照常前进。"""
    city = reqs.destination.value
    if not city:
        return "UTC"
    try:
        return provider.timezone_of(city)
    except ProviderError:
        return "UTC"


def _lookup(provider, query: str, city: str, cache: dict) -> PoiResolution:
    if query in cache:
        return cache[query]
    try:
        hits = provider.search_poi(query, city)
    except ProviderError:
        hits = []
    if len(hits) == 1:
        res: PoiResolution = Resolved(hits[0])
    elif len(hits) > 1:
        res = Ambiguous(hits)  # 不擅自挑第一条
    else:
        res = NotFound(query)
    cache[query] = res
    return res


def resolve(
    itin: Itinerary, reqs: Requirements, provider: GeoProvider, tz: str
) -> FactSnapshot:
    city = reqs.destination.value or ""
    zone = ZoneInfo(tz)
    cache: dict[str, PoiResolution] = {}
    gaps: list[Gap] = []

    # ---- 活动侧 POI ----
    poi_by_activity: dict[str, PoiResolution] = {}
    for act in itin.all_activities():
        res = _lookup(provider, act.poi_query, city, cache)
        poi_by_activity[act.id] = res
        if isinstance(res, Ambiguous):
            gaps.append(
                Gap(
                    GapKind.AMBIGUOUS_POI,
                    act.id,
                    f"「{act.poi_query}」匹配到 " f"{len(res.candidates)} 个同名地点",
                )
            )
        elif isinstance(res, NotFound):
            gaps.append(
                Gap(GapKind.POI_NOT_FOUND, act.id, f"查不到「{act.poi_query}」")
            )

    # ---- 约束侧 POI（must_visit / avoid）----
    constraint_pois: dict[str, PoiResolution] = {}
    for query in (reqs.must_visit.value or []) + (reqs.avoid.value or []):
        if query in constraint_pois:
            continue
        res = _lookup(provider, query, city, cache)
        constraint_pois[query] = res
        if not isinstance(res, Resolved):
            gaps.append(
                Gap(
                    GapKind.AMBIGUOUS_CONSTRAINT,
                    query,
                    f"约束「{query}」未能唯一解析",
                )
            )

    # ---- 相邻活动之间的路线 ----
    routes: list[RouteFact] = []
    for day in itin.days:
        for prev, nxt in zip(day.activities, day.activities[1:]):
            a = poi_by_activity.get(prev.id)
            b = poi_by_activity.get(nxt.id)
            subject = f"{prev.id}->{nxt.id}"
            if not (isinstance(a, Resolved) and isinstance(b, Resolved)):
                gaps.append(
                    Gap(
                        GapKind.ROUTE_UNAVAILABLE,
                        subject,
                        "两端 POI 未能唯一解析，无法测算",
                    )
                )
                continue
            depart_at = datetime.combine(day.date, prev.end, tzinfo=zone)
            try:
                obs = provider.route(
                    a.fact.coords, b.fact.coords, DEFAULT_MODE, depart_at
                )
            except ProviderError as e:
                gaps.append(Gap(GapKind.ROUTE_UNAVAILABLE, subject, str(e)))
                continue
            routes.append(
                RouteFact(
                    day_id=day.id,
                    from_activity_id=prev.id,
                    to_activity_id=nxt.id,
                    depart_at=depart_at,
                    mode=obs.mode,
                    duration_min=obs.duration_min,
                    distance_m=obs.distance_m,
                    polyline=obs.polyline,
                    source=obs.source,
                    fetched_at=obs.fetched_at,
                )
            )

    return FactSnapshot(
        poi_by_activity=poi_by_activity,
        constraint_pois=constraint_pois,
        routes=routes,
        weather={},  # v1 不接天气，见 spec §2
        trip_timezone=tz,  # 抄一份供回放核对
        resolved_at=datetime.now(zone),
        gaps=gaps,
    )
