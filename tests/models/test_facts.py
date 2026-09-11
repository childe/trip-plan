from datetime import datetime, timedelta, timezone

import pytest

from tripplan.models.common import LatLng, TravelMode
from tripplan.models.facts import (
    Ambiguous,
    FactSnapshot,
    Gap,
    GapKind,
    NotFound,
    PoiFact,
    Resolved,
    RouteFact,
)

JST = timezone(timedelta(hours=9))


def _poi(pid: str) -> PoiFact:
    return PoiFact(
        id=pid,
        name=pid,
        coords=LatLng(35.0, 135.7),
        opening_hours=None,
        ticket=None,
        source="fake",
        fetched_at=datetime(2026, 9, 1, tzinfo=JST),
    )


def _route(from_id: str, to_id: str, minutes: int) -> RouteFact:
    return RouteFact(
        day_id="d1",
        from_activity_id=from_id,
        to_activity_id=to_id,
        depart_at=datetime(2026, 10, 1, 11, 0, tzinfo=JST),
        mode=TravelMode.TRANSIT,
        duration_min=minutes,
        distance_m=4200,
        polyline="aaa|bbb",
        source="amap:direction/transit",
        fetched_at=datetime(2026, 9, 1, tzinfo=JST),
    )


def _snap(**kw) -> FactSnapshot:
    base = dict(
        poi_by_activity={},
        constraint_pois={},
        routes=[],
        weather={},
        trip_timezone="Asia/Tokyo",
        resolved_at=datetime(2026, 9, 1, tzinfo=JST),
        gaps=[],
    )
    base.update(kw)
    return FactSnapshot(**base)


def test_route_lookup_by_day_and_both_endpoints():
    snap = _snap(routes=[_route("d1a1", "d1a2", 40)])
    assert snap.route("d1", "d1a1", "d1a2").duration_min == 40
    assert snap.route("d1", "d1a2", "d1a1") is None  # 方向不同
    assert snap.route("d2", "d1a1", "d1a2") is None  # 天不同


def test_poi_id_only_for_unambiguously_resolved():
    snap = _snap(
        poi_by_activity={
            "d1a1": Resolved(_poi("B001")),
            "d1a2": Ambiguous([_poi("B002"), _poi("B003")]),
            "d1a3": NotFound("不存在的地方"),
        }
    )
    assert snap.poi_id_for("d1a1") == "B001"
    assert snap.poi_id_for("d1a2") is None  # ★ 歧义时不擅自挑一个
    assert snap.poi_id_for("d1a3") is None
    assert snap.poi_id_for("nope") is None


def test_constraint_poi_id_follows_same_rule():
    snap = _snap(
        constraint_pois={
            "清水寺": Resolved(_poi("B001")),
            "某某寺": Ambiguous([_poi("B002"), _poi("B003")]),
        }
    )
    assert snap.constraint_poi_id("清水寺") == "B001"
    assert snap.constraint_poi_id("某某寺") is None
    assert snap.constraint_poi_id("没提过") is None


def test_resolved_poi_ids_collects_only_resolved():
    snap = _snap(
        poi_by_activity={
            "d1a1": Resolved(_poi("B001")),
            "d1a2": NotFound("x"),
            "d1a3": Resolved(_poi("B009")),
        }
    )
    assert snap.resolved_poi_ids() == {"B001", "B009"}


def test_gaps_record_what_could_not_be_verified():
    snap = _snap(
        gaps=[
            Gap(kind=GapKind.ROUTE_UNAVAILABLE, subject="d1a1->d1a2", detail="高德限流")
        ]
    )
    assert snap.gaps[0].kind is GapKind.ROUTE_UNAVAILABLE


def test_snapshot_is_frozen():
    snap = _snap()
    with pytest.raises(Exception):
        snap.trip_timezone = "Europe/Paris"
