from datetime import date, time

from tripplan.models.itinerary import (
    Activity,
    Angle,
    Category,
    Day,
    Itinerary,
    assign_ids,
)


def _act(query: str, start: str, end: str) -> Activity:
    h1, m1 = map(int, start.split(":"))
    h2, m2 = map(int, end.split(":"))
    return Activity(
        id="",
        day_id="",
        poi_query=query,
        start=time(h1, m1),
        end=time(h2, m2),
        category=Category.SIGHT,
        cost=None,
        indoor=False,
        note="",
    )


def _itin() -> Itinerary:
    return Itinerary(
        angle=Angle(key="A", title="古寺巡礼", description="以世界遗产为主线"),
        days=[
            Day(
                id="",
                date=date(2026, 10, 1),
                activities=[
                    _act("清水寺", "09:00", "11:00"),
                    _act("八坂神社", "13:00", "14:30"),
                ],
                lodging="京都站附近",
            ),
            Day(
                id="",
                date=date(2026, 10, 2),
                activities=[_act("金阁寺", "09:30", "11:00")],
                lodging="京都站附近",
            ),
        ],
        issues=[],
    )


def test_assign_ids_gives_every_day_and_activity_a_stable_id():
    out = assign_ids(_itin())
    assert [d.id for d in out.days] == ["d1", "d2"]
    assert [a.id for a in out.days[0].activities] == ["d1a1", "d1a2"]
    assert [a.id for a in out.days[1].activities] == ["d2a1"]


def test_assign_ids_backfills_day_id_on_activities():
    out = assign_ids(_itin())
    assert all(a.day_id == "d1" for a in out.days[0].activities)
    assert all(a.day_id == "d2" for a in out.days[1].activities)


def test_activity_ids_are_unique_across_days():
    """跨天唯一——列表下标做不到这点，RouteFact 依赖它定位两端。"""
    out = assign_ids(_itin())
    ids = [a.id for a in out.all_activities()]
    assert len(ids) == len(set(ids))


def test_assign_ids_is_idempotent():
    once = assign_ids(_itin())
    twice = assign_ids(once)
    assert [a.id for a in twice.all_activities()] == [
        a.id for a in once.all_activities()
    ]


def test_lookup_activity_by_id():
    out = assign_ids(_itin())
    assert out.activity("d1a2").poi_query == "八坂神社"
    assert out.activity("nope") is None


def test_all_activities_walks_days_in_order():
    out = assign_ids(_itin())
    assert [a.poi_query for a in out.all_activities()] == [
        "清水寺",
        "八坂神社",
        "金阁寺",
    ]
