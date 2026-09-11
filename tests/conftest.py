from datetime import date, datetime, time, timedelta, timezone

import pytest

from tripplan.models.common import Field, LatLng, Origin, TravelMode
from tripplan.models.facts import (
    FactSnapshot,
    Gap,
    GapKind,
    PoiFact,
    Resolved,
    RouteFact,
)
from tripplan.models.itinerary import Activity, Angle, Category, Day, Itinerary
from tripplan.models.requirements import DateRange, Party, Requirements, Transfer

JST = timezone(timedelta(hours=9))
TZ = "Asia/Tokyo"


@pytest.fixture
def mk():
    return _Builder()


class _Builder:
    """构造行程与快照的小工具，让每条规则的用例只关心自己在意的那一点。"""

    def act(
        self,
        aid,
        day_id,
        start,
        end,
        query="某地",
        category=Category.SIGHT,
        cost=None,
        indoor=False,
    ):
        h1, m1 = map(int, start.split(":"))
        h2, m2 = map(int, end.split(":"))
        return Activity(
            id=aid,
            day_id=day_id,
            poi_query=query,
            start=time(h1, m1),
            end=time(h2, m2),
            category=category,
            cost=cost,
            indoor=indoor,
            note="",
        )

    def day(self, day_id, d: date, activities):
        return Day(id=day_id, date=d, activities=activities)

    def itin(self, days):
        return Itinerary(angle=Angle("A", "测试方案", ""), days=days)

    def reqs(
        self,
        start=date(2026, 10, 1),
        end=date(2026, 10, 1),
        must_visit=None,
        avoid=None,
        arrival=None,
        departure=None,
        party=2,
        **kw,
    ):
        base = dict(
            destination=Field("京都", Origin.USER),
            dates=Field(DateRange(start, end), Origin.USER),
            party=Field(Party(adults=party), Origin.USER),
        )
        if must_visit is not None:
            base["must_visit"] = Field(must_visit, Origin.USER)
        if avoid is not None:
            base["avoid"] = Field(avoid, Origin.USER)
        if arrival is not None:
            base["arrival"] = Field(Transfer(arrival, "flight"), Origin.USER)
        if departure is not None:
            base["departure"] = Field(Transfer(departure, "flight"), Origin.USER)
        base.update(kw)
        return Requirements(**base)

    def poi(self, pid, name=None):
        return PoiFact(
            id=pid,
            name=name or pid,
            coords=LatLng(35.0, 135.7),
            opening_hours=None,
            ticket=None,
            source="fake",
            fetched_at=datetime(2026, 9, 1, tzinfo=JST),
        )

    def route(self, day_id, a, b, minutes, depart="11:00", on=date(2026, 10, 1)):
        h, m = map(int, depart.split(":"))
        return RouteFact(
            day_id=day_id,
            from_activity_id=a,
            to_activity_id=b,
            depart_at=datetime(on.year, on.month, on.day, h, m, tzinfo=JST),
            mode=TravelMode.TRANSIT,
            duration_min=minutes,
            distance_m=1000 * minutes,
            polyline="poly",
            source="fake",
            fetched_at=datetime(2026, 9, 1, tzinfo=JST),
        )

    def facts(
        self, poi_by_activity=None, constraint_pois=None, routes=None, gaps=None, tz=TZ
    ):
        return FactSnapshot(
            poi_by_activity=poi_by_activity or {},
            constraint_pois=constraint_pois or {},
            routes=routes or [],
            weather={},
            trip_timezone=tz,
            resolved_at=datetime(2026, 9, 1, tzinfo=JST),
            gaps=gaps or [],
        )

    def gap(self, kind: GapKind, subject: str, detail: str = "x"):
        return Gap(kind=kind, subject=subject, detail=detail)

    def resolved(self, pid):
        return Resolved(self.poi(pid))
