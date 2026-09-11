from datetime import date, datetime

from tripplan.models.common import Field, Origin
from tripplan.models.facts import Ambiguous, GapKind, NotFound, Resolved
from tripplan.providers.fake import FakeProvider
from tripplan.validation.resolver import resolve, resolve_timezone

D1 = date(2026, 10, 1)
TZ = "Asia/Tokyo"


def _provider(**kw):
    base = dict(
        pois={
            "清水寺": [("B001", 34.9949, 135.7850)],
            "八坂神社": [("B002", 35.0036, 135.7786)],
        }
    )
    base.update(kw)
    return FakeProvider(**base)


def _gap_kinds(facts):
    return {g.kind for g in facts.gaps}


def test_resolves_each_activity_to_a_poi(mk):
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺"),
                    mk.act("d1a2", "d1", "13:00", "14:00", query="八坂神社"),
                ],
            )
        ]
    )
    facts = resolve(itin, mk.reqs(), _provider(), TZ)
    assert facts.poi_id_for("d1a1") == "B001"
    assert facts.poi_id_for("d1a2") == "B002"


def test_ambiguous_poi_is_recorded_not_guessed(mk):
    """同名多个时不擅自挑第一条——挑错了下游全部建立在错坐标上。"""
    provider = _provider(pois={"某某寺": [("B1", 35.0, 135.0), ("B2", 35.5, 135.5)]})
    itin = mk.itin(
        [mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00", query="某某寺")])]
    )
    facts = resolve(itin, mk.reqs(), provider, TZ)
    assert isinstance(facts.poi_by_activity["d1a1"], Ambiguous)
    assert facts.poi_id_for("d1a1") is None
    assert GapKind.AMBIGUOUS_POI in _gap_kinds(facts)


def test_missing_poi_is_recorded(mk):
    itin = mk.itin(
        [mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00", query="虚构地点")])]
    )
    facts = resolve(itin, mk.reqs(), _provider(), TZ)
    assert isinstance(facts.poi_by_activity["d1a1"], NotFound)
    assert GapKind.POI_NOT_FOUND in _gap_kinds(facts)


def test_routes_are_computed_between_adjacent_activities(mk):
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺"),
                    mk.act("d1a2", "d1", "13:00", "14:00", query="八坂神社"),
                ],
            )
        ]
    )
    facts = resolve(itin, mk.reqs(), _provider(), TZ)
    route = facts.route("d1", "d1a1", "d1a2")
    assert route is not None
    assert route.polyline


def test_route_depart_at_uses_previous_activity_end_in_trip_timezone(mk):
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺"),
                    mk.act("d1a2", "d1", "13:00", "14:00", query="八坂神社"),
                ],
            )
        ]
    )
    facts = resolve(itin, mk.reqs(), _provider(), TZ)
    depart = facts.route("d1", "d1a1", "d1a2").depart_at
    assert depart.hour == 11 and depart.minute == 0
    assert depart.utcoffset().total_seconds() == 9 * 3600


def test_route_skipped_when_either_endpoint_unresolved(mk):
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺"),
                    mk.act("d1a2", "d1", "13:00", "14:00", query="虚构地点"),
                ],
            )
        ]
    )
    facts = resolve(itin, mk.reqs(), _provider(), TZ)
    assert facts.route("d1", "d1a1", "d1a2") is None
    assert GapKind.ROUTE_UNAVAILABLE in _gap_kinds(facts)


def test_route_failure_becomes_a_gap_not_an_exception(mk):
    """高德限流不该让整条候选线炸掉——记 gap，让规则 #2 降级。"""
    provider = _provider(fail_routes={((34.9949, 135.785), (35.0036, 135.7786))})
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺"),
                    mk.act("d1a2", "d1", "13:00", "14:00", query="八坂神社"),
                ],
            )
        ]
    )
    facts = resolve(itin, mk.reqs(), provider, TZ)
    assert facts.route("d1", "d1a1", "d1a2") is None
    assert GapKind.ROUTE_UNAVAILABLE in _gap_kinds(facts)


def test_constraints_are_resolved_too(mk):
    """约束侧不解析，「按 POI id 匹配」就无从谈起。"""
    itin = mk.itin([mk.day("d1", D1, [])])
    reqs = mk.reqs(must_visit=["清水寺"], avoid=["八坂神社"])
    facts = resolve(itin, reqs, _provider(), TZ)
    assert facts.constraint_poi_id("清水寺") == "B001"
    assert facts.constraint_poi_id("八坂神社") == "B002"


def test_ambiguous_constraint_gets_its_own_gap_kind(mk):
    provider = _provider(pois={"某某寺": [("B1", 35.0, 135.0), ("B2", 35.5, 135.5)]})
    itin = mk.itin([mk.day("d1", D1, [])])
    facts = resolve(itin, mk.reqs(must_visit=["某某寺"]), provider, TZ)
    assert GapKind.AMBIGUOUS_CONSTRAINT in _gap_kinds(facts)


def test_snapshot_records_the_timezone_it_was_given(mk):
    """快照只抄一份供回放核对，不做第二个解析点。"""
    itin = mk.itin([mk.day("d1", D1, [])])
    provider = _provider()
    facts = resolve(itin, mk.reqs(), provider, "Europe/Paris")
    assert facts.trip_timezone == "Europe/Paris"
    assert "timezone_of" not in provider.call_log  # ★ 没有自己去查


def test_repeated_poi_query_hits_provider_once(mk):
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "10:00", query="清水寺"),
                    mk.act("d1a2", "d1", "12:00", "13:00", query="清水寺"),
                ],
            )
        ]
    )
    provider = _provider()
    resolve(itin, mk.reqs(), provider, TZ)
    assert provider.call_log.count("search_poi") == 1


def test_resolve_timezone_uses_destination(mk):
    provider = _provider()
    assert resolve_timezone(mk.reqs(), provider) == "Asia/Tokyo"


def test_resolve_timezone_falls_back_to_utc_when_unknown(mk):
    reqs = mk.reqs(destination=Field("虚构城", Origin.MODEL))
    assert resolve_timezone(reqs, _provider()) == "UTC"


def test_provider_failure_on_poi_lookup_preserves_error_detail(mk):
    """服务限流不是「查无此地」——detail 要带上 provider 的原始报错，
    不能和真查不到时用一样的泛化文案，否则用户会去改一个本来没错的地名。"""
    provider = _provider(fail_pois={"清水寺"})
    itin = mk.itin(
        [mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺")])]
    )
    facts = resolve(itin, mk.reqs(), provider, TZ)
    assert isinstance(facts.poi_by_activity["d1a1"], NotFound)
    gap = next(g for g in facts.gaps if g.subject == "d1a1")
    assert gap.kind == GapKind.POI_NOT_FOUND
    assert gap.detail == "POI 查询失败：清水寺"


def test_cross_side_cache_hit_for_shared_place_name(mk):
    """同一地名既是某天的活动，又出现在 must_visit 里——两侧共用一份缓存。"""
    itin = mk.itin(
        [mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺")])]
    )
    provider = _provider()
    resolve(itin, mk.reqs(must_visit=["清水寺"]), provider, TZ)
    assert provider.call_log.count("search_poi") == 1


def test_route_skip_gap_names_which_endpoint_failed(mk):
    """只有一端没解析出来时，detail 要点名是哪一端，不能笼统说「两端」。"""
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00", query="虚构地点"),
                    mk.act("d1a2", "d1", "13:00", "14:00", query="八坂神社"),
                ],
            )
        ]
    )
    facts = resolve(itin, mk.reqs(), _provider(), TZ)
    gap = next(g for g in facts.gaps if g.kind == GapKind.ROUTE_UNAVAILABLE)
    assert "虚构地点" in gap.detail
    assert "八坂神社" not in gap.detail
