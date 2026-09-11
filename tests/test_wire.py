import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from tripplan.models.common import Confidence, Field, LatLng, Money, Origin, TravelMode
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
from tripplan.models.issue import ActivityRef, Issue, Severity, Source
from tripplan.models.itinerary import Activity, Angle, Category, Day, Itinerary
from tripplan.models.requirements import (
    Basis,
    BudgetSpec,
    CostKind,
    DateRange,
    Pace,
    Party,
    Requirements,
    Transfer,
)
from tripplan.state import CandidateSlot, SlotStatus, Stage, TripState
from tripplan.wire import (
    FORMAT_VERSION,
    MIGRATIONS,
    UnsupportedVersion,
    dumps,
    loads,
)

JST = timezone(timedelta(hours=9))


def _full_state() -> TripState:
    poi = PoiFact(
        id="B001",
        name="清水寺",
        coords=LatLng(34.99, 135.78),
        opening_hours="06:00-18:00",
        ticket=Money(Decimal("400"), "JPY", Confidence.ESTIMATED, "llm"),
        source="amap",
        fetched_at=datetime(2026, 9, 1, 8, tzinfo=JST),
    )
    itin = Itinerary(
        angle=Angle("A", "古寺巡礼", "世界遗产主线"),
        days=[
            Day(
                id="d1",
                date=date(2026, 10, 1),
                lodging="京都站",
                activities=[
                    Activity(
                        id="d1a1",
                        day_id="d1",
                        poi_query="清水寺",
                        start=time(9, 0),
                        end=time(11, 0),
                        category=Category.SIGHT,
                        cost=Money(Decimal("400"), "JPY", Confidence.ESTIMATED, "llm"),
                        indoor=False,
                        note="清晨人少",
                    ),
                ],
            )
        ],
        issues=[
            Issue(
                Severity.WARNING,
                Source.RULE,
                "R7",
                "略赶",
                where=ActivityRef("d1", "d1a1"),
            )
        ],
    )
    facts = FactSnapshot(
        poi_by_activity={
            "d1a1": Resolved(poi),
            "d1a2": Ambiguous([poi]),
            "d1a3": NotFound("某处"),
        },
        constraint_pois={"清水寺": Resolved(poi)},
        routes=[
            RouteFact(
                "d1",
                "d1a1",
                "d1a2",
                datetime(2026, 10, 1, 11, tzinfo=JST),
                TravelMode.TRANSIT,
                40,
                4200,
                "poly",
                "amap",
                datetime(2026, 9, 1, tzinfo=JST),
            )
        ],
        weather={},
        trip_timezone="Asia/Tokyo",
        resolved_at=datetime(2026, 9, 1, tzinfo=JST),
        gaps=[Gap(GapKind.POI_NOT_FOUND, "d1a3", "没匹配上")],
    )
    reqs = Requirements(
        destination=Field("京都", Origin.USER, confirmed=True),
        dates=Field(DateRange(date(2026, 10, 1), date(2026, 10, 5)), Origin.USER),
        party=Field(Party(2), Origin.USER),
        arrival=Field(
            Transfer(datetime(2026, 10, 1, 9, tzinfo=JST), "flight"), Origin.USER
        ),
        budget=Field(
            BudgetSpec(
                Decimal("15000"),
                "CNY",
                Basis.TOTAL,
                frozenset({CostKind.TICKET, CostKind.MEAL}),
            ),
            Origin.MODEL,
            rationale="按人均 7500 推断",
        ),
        pace=Field(Pace.RELAXED, Origin.MODEL),
        styles=Field(["美食", "历史"], Origin.USER),
    )
    s = TripState.new("十一去京都玩5天", run_id="r-001")
    s.revision = 4
    s.stage = Stage.AWAIT_CHOICE
    s.requirements = reqs
    s.trip_timezone = "Asia/Tokyo"
    s.candidates = [
        CandidateSlot(Angle("A", "古寺巡礼", ""), itin, facts, SlotStatus.OK),
        CandidateSlot(
            Angle("B", "美食优先", ""), None, None, SlotStatus.FAILED, "高德限流"
        ),
    ]
    s.seeds = {"A": itin}
    s.issues = [Issue.from_human("第2天太赶了")]
    return s


def test_roundtrip_preserves_everything():
    original = _full_state()
    restored = loads(dumps(original))
    assert dumps(restored) == dumps(original)


def test_roundtrip_preserves_types_not_just_shape():
    restored = loads(dumps(_full_state()))
    reqs = restored.requirements
    assert isinstance(reqs.budget.value.amount, Decimal)
    assert reqs.budget.value.basis is Basis.TOTAL
    assert isinstance(reqs.budget.value.includes, frozenset)
    assert reqs.dates.value.start == date(2026, 10, 1)
    assert reqs.arrival.value.at.tzinfo is not None
    act = restored.candidates[0].itinerary.days[0].activities[0]
    assert act.start == time(9, 0)
    assert isinstance(act.cost.amount, Decimal)
    assert act.category is Category.SIGHT


def test_field_origin_and_confirmed_both_survive():
    restored = loads(dumps(_full_state()))
    assert restored.requirements.destination.confirmed is True
    assert restored.requirements.budget.origin is Origin.MODEL
    assert restored.requirements.budget.confirmed is False
    assert restored.requirements.budget.rationale == "按人均 7500 推断"


def test_poi_resolution_union_is_tagged():
    raw = json.loads(dumps(_full_state()))
    pois = raw["candidates"][0]["facts"]["poi_by_activity"]
    assert pois["d1a1"]["kind"] == "Resolved"
    assert pois["d1a2"]["kind"] == "Ambiguous"
    assert pois["d1a3"]["kind"] == "NotFound"


def test_poi_resolution_union_decodes_back_to_right_classes():
    restored = loads(dumps(_full_state()))
    pois = restored.candidates[0].facts.poi_by_activity
    assert isinstance(pois["d1a1"], Resolved)
    assert isinstance(pois["d1a2"], Ambiguous)
    assert isinstance(pois["d1a3"], NotFound)


def test_decimal_is_string_never_float():
    raw = json.loads(dumps(_full_state()))
    amount = raw["requirements"]["budget"]["value"]["amount"]
    assert isinstance(amount, str)
    assert amount == "15000"


def test_datetime_is_iso_with_offset():
    raw = json.loads(dumps(_full_state()))
    at = raw["requirements"]["arrival"]["value"]["at"]
    assert at.startswith("2026-10-01T09:00:00")
    assert at.endswith("+09:00")


def test_enum_serialized_by_name_not_ordinal():
    raw = json.loads(dumps(_full_state()))
    assert raw["stage"] == "AWAIT_CHOICE"
    assert raw["candidates"][0]["status"] == "OK"


def test_frozenset_is_sorted_list_for_stable_bytes():
    raw = json.loads(dumps(_full_state()))
    includes = raw["requirements"]["budget"]["value"]["includes"]
    assert includes == sorted(includes)


def test_routes_are_a_list_not_tuple_keyed_dict():
    raw = json.loads(dumps(_full_state()))
    assert isinstance(raw["candidates"][0]["facts"]["routes"], list)


def test_format_version_is_written():
    raw = json.loads(dumps(_full_state()))
    assert raw["format_version"] == FORMAT_VERSION


def test_future_version_is_rejected_not_guessed():
    raw = json.loads(dumps(_full_state()))
    raw["format_version"] = FORMAT_VERSION + 1
    with pytest.raises(UnsupportedVersion):
        loads(json.dumps(raw))


def test_older_version_runs_migrations_then_parses():
    """迁移表 v1 时为空，但入口分支必须存在，否则第一次改结构就全变砖。"""
    raw = json.loads(dumps(_full_state()))
    raw["format_version"] = 0
    MIGRATIONS[0] = lambda d: {**d, "format_version": 1}
    try:
        restored = loads(json.dumps(raw))
        assert restored.revision == 4
    finally:
        del MIGRATIONS[0]


def test_slot_without_itinerary_roundtrips():
    restored = loads(dumps(_full_state()))
    b = restored.slot("B")
    assert b.itinerary is None and b.facts is None
    assert b.status is SlotStatus.FAILED
    assert b.detail == "高德限流"
