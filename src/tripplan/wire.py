"""state.json 的 wire format。

「可 JSON 序列化」不是自动成立的：datetime/Decimal/Enum/frozenset/联合类型
都需要显式处理。这里定死表示形式，不留给调用方临场发挥。
"""

import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Callable

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
    WeatherFact,
)
from tripplan.models.issue import ActivityRef, DayRef, Issue, Severity, Source
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

FORMAT_VERSION = 1

#: 版本 N -> 把 N 的结构升到 N+1 的函数。v1 时为空，但 loads 的分支必须在。
MIGRATIONS: dict[int, Callable[[dict], dict]] = {}


class UnsupportedVersion(Exception):
    pass


# ---------- 标量 ----------


def _dt(v: datetime | None) -> str | None:
    if v is None:
        return None
    if v.tzinfo is None:
        raise ValueError(f"datetime 必须 tz-aware: {v!r}")
    return v.isoformat()


def _un_dt(v: str | None) -> datetime | None:
    return None if v is None else datetime.fromisoformat(v)


def _money(m: Money | None) -> dict | None:
    if m is None:
        return None
    return {
        "amount": str(m.amount),
        "currency": m.currency,
        "confidence": m.confidence.value,
        "source": m.source,
    }


def _un_money(d: dict | None) -> Money | None:
    if d is None:
        return None
    return Money(
        Decimal(d["amount"]), d["currency"], Confidence(d["confidence"]), d["source"]
    )


def _field(f: Field, enc) -> dict:
    return {
        "value": None if f.value is None else enc(f.value),
        "origin": None if f.origin is None else f.origin.value,
        "confirmed": f.confirmed,
        "rationale": f.rationale,
    }


def _un_field(d: dict, dec) -> Field:
    return Field(
        value=None if d["value"] is None else dec(d["value"]),
        origin=None if d["origin"] is None else Origin(d["origin"]),
        confirmed=d["confirmed"],
        rationale=d["rationale"],
    )


# ---------- 需求 ----------


def _budget(b: BudgetSpec) -> dict:
    return {
        "amount": str(b.amount),
        "currency": b.currency,
        "basis": b.basis.value,
        "includes": sorted(k.value for k in b.includes),  # 排序保证字节稳定
    }


def _un_budget(d: dict) -> BudgetSpec:
    return BudgetSpec(
        Decimal(d["amount"]),
        d["currency"],
        Basis(d["basis"]),
        frozenset(CostKind(k) for k in d["includes"]),
    )


def _requirements(r: Requirements) -> dict:
    return {
        "destination": _field(r.destination, str),
        "dates": _field(
            r.dates, lambda v: {"start": v.start.isoformat(), "end": v.end.isoformat()}
        ),
        "party": _field(
            r.party,
            lambda v: {
                "adults": v.adults,
                "children": v.children,
                "seniors": v.seniors,
            },
        ),
        "arrival": _field(r.arrival, lambda v: {"at": _dt(v.at), "mode": v.mode}),
        "departure": _field(r.departure, lambda v: {"at": _dt(v.at), "mode": v.mode}),
        "budget": _field(r.budget, _budget),
        "styles": _field(r.styles, list),
        "pace": _field(r.pace, lambda v: v.value),
        "must_visit": _field(r.must_visit, list),
        "avoid": _field(r.avoid, list),
        "lodging_area": _field(r.lodging_area, str),
        "constraints": _field(r.constraints, list),
    }


def _un_requirements(d: dict) -> Requirements:
    return Requirements(
        destination=_un_field(d["destination"], str),
        dates=_un_field(
            d["dates"],
            lambda v: DateRange(
                date.fromisoformat(v["start"]), date.fromisoformat(v["end"])
            ),
        ),
        party=_un_field(d["party"], lambda v: Party(**v)),
        arrival=_un_field(d["arrival"], lambda v: Transfer(_un_dt(v["at"]), v["mode"])),
        departure=_un_field(
            d["departure"], lambda v: Transfer(_un_dt(v["at"]), v["mode"])
        ),
        budget=_un_field(d["budget"], _un_budget),
        styles=_un_field(d["styles"], list),
        pace=_un_field(d["pace"], Pace),
        must_visit=_un_field(d["must_visit"], list),
        avoid=_un_field(d["avoid"], list),
        lodging_area=_un_field(d["lodging_area"], str),
        constraints=_un_field(d["constraints"], list),
    )


# ---------- 行程 ----------


def _issue(i: Issue) -> dict:
    """i.where 是判别式联合，必须显式穷举——见 _resolution 的同一套规则。"""
    if i.where is None:
        where = None
    elif isinstance(i.where, ActivityRef):
        where = {
            "kind": "ActivityRef",
            "day_id": i.where.day_id,
            "activity_id": i.where.activity_id,
        }
    elif isinstance(i.where, DayRef):
        where = {"kind": "DayRef", "day_id": i.where.day_id}
    else:
        raise TypeError(f"未知的 Issue.where: {i.where!r}")
    return {
        "severity": i.severity.value,
        "source": i.source.value,
        "code": i.code,
        "message": i.message,
        "where": where,
    }


def _un_issue(d: dict) -> Issue:
    w = d["where"]
    if w is None:
        where = None
    elif w["kind"] == "ActivityRef":
        where = ActivityRef(w["day_id"], w["activity_id"])
    elif w["kind"] == "DayRef":
        where = DayRef(w["day_id"])
    else:
        raise UnsupportedVersion(f"未知的 Issue.where kind: {w['kind']}")
    return Issue(
        Severity(d["severity"]), Source(d["source"]), d["code"], d["message"], where
    )


def _itinerary(it: Itinerary | None) -> dict | None:
    if it is None:
        return None
    return {
        "angle": {
            "key": it.angle.key,
            "title": it.angle.title,
            "description": it.angle.description,
        },
        "days": [
            {
                "id": d.id,
                "date": d.date.isoformat(),
                "lodging": d.lodging,
                "activities": [
                    {
                        "id": a.id,
                        "day_id": a.day_id,
                        "poi_query": a.poi_query,
                        "start": a.start.isoformat(),
                        "end": a.end.isoformat(),
                        "category": a.category.value,
                        "cost": _money(a.cost),
                        "indoor": a.indoor,
                        "note": a.note,
                    }
                    for a in d.activities
                ],
            }
            for d in it.days
        ],
        "issues": [_issue(i) for i in it.issues],
    }


def _un_itinerary(d: dict | None) -> Itinerary | None:
    if d is None:
        return None
    return Itinerary(
        angle=Angle(**d["angle"]),
        days=[
            Day(
                id=x["id"],
                date=date.fromisoformat(x["date"]),
                lodging=x["lodging"],
                activities=[
                    Activity(
                        id=a["id"],
                        day_id=a["day_id"],
                        poi_query=a["poi_query"],
                        start=time.fromisoformat(a["start"]),
                        end=time.fromisoformat(a["end"]),
                        category=Category(a["category"]),
                        cost=_un_money(a["cost"]),
                        indoor=a["indoor"],
                        note=a["note"],
                    )
                    for a in x["activities"]
                ],
            )
            for x in d["days"]
        ],
        issues=[_un_issue(i) for i in d["issues"]],
    )


# ---------- 事实 ----------


def _poi(p: PoiFact) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "coords": {"lat": p.coords.lat, "lng": p.coords.lng},
        "opening_hours": p.opening_hours,
        "ticket": _money(p.ticket),
        "source": p.source,
        "fetched_at": _dt(p.fetched_at),
    }


def _un_poi(d: dict) -> PoiFact:
    return PoiFact(
        d["id"],
        d["name"],
        LatLng(**d["coords"]),
        d["opening_hours"],
        _un_money(d["ticket"]),
        d["source"],
        _un_dt(d["fetched_at"]),
    )


def _resolution(r) -> dict:
    """联合类型必须显式打 kind 标签——靠字段形状去猜会在字段可选时崩。"""
    match r:
        case Resolved(fact):
            return {"kind": "Resolved", "fact": _poi(fact)}
        case Ambiguous(cands):
            return {"kind": "Ambiguous", "candidates": [_poi(c) for c in cands]}
        case NotFound(q):
            return {"kind": "NotFound", "query": q}
    raise TypeError(f"未知的 PoiResolution: {r!r}")


def _un_resolution(d: dict):
    match d["kind"]:
        case "Resolved":
            return Resolved(_un_poi(d["fact"]))
        case "Ambiguous":
            return Ambiguous([_un_poi(c) for c in d["candidates"]])
        case "NotFound":
            return NotFound(d["query"])
    raise UnsupportedVersion(f"未知的 PoiResolution kind: {d['kind']}")


def _facts(f: FactSnapshot | None) -> dict | None:
    if f is None:
        return None
    return {
        "poi_by_activity": {k: _resolution(v) for k, v in f.poi_by_activity.items()},
        "constraint_pois": {k: _resolution(v) for k, v in f.constraint_pois.items()},
        "routes": [
            {
                "day_id": r.day_id,
                "from_activity_id": r.from_activity_id,
                "to_activity_id": r.to_activity_id,
                "depart_at": _dt(r.depart_at),
                "mode": r.mode.value,
                "duration_min": r.duration_min,
                "distance_m": r.distance_m,
                "polyline": r.polyline,
                "source": r.source,
                "fetched_at": _dt(r.fetched_at),
            }
            for r in f.routes
        ],
        "weather": {
            k: {
                "date_iso": w.date_iso,
                "summary": w.summary,
                "temp_c_min": w.temp_c_min,
                "temp_c_max": w.temp_c_max,
                "source": w.source,
            }
            for k, w in f.weather.items()
        },
        "trip_timezone": f.trip_timezone,
        "resolved_at": _dt(f.resolved_at),
        "gaps": [
            {"kind": g.kind.value, "subject": g.subject, "detail": g.detail}
            for g in f.gaps
        ],
    }


def _un_facts(d: dict | None) -> FactSnapshot | None:
    if d is None:
        return None
    return FactSnapshot(
        poi_by_activity={k: _un_resolution(v) for k, v in d["poi_by_activity"].items()},
        constraint_pois={k: _un_resolution(v) for k, v in d["constraint_pois"].items()},
        routes=[
            RouteFact(
                r["day_id"],
                r["from_activity_id"],
                r["to_activity_id"],
                _un_dt(r["depart_at"]),
                TravelMode(r["mode"]),
                r["duration_min"],
                r["distance_m"],
                r["polyline"],
                r["source"],
                _un_dt(r["fetched_at"]),
            )
            for r in d["routes"]
        ],
        weather={k: WeatherFact(**w) for k, w in d["weather"].items()},
        trip_timezone=d["trip_timezone"],
        resolved_at=_un_dt(d["resolved_at"]),
        gaps=[Gap(GapKind(g["kind"]), g["subject"], g["detail"]) for g in d["gaps"]],
    )


# ---------- 顶层 ----------


def encode_state(s: TripState) -> dict:
    return {
        "format_version": FORMAT_VERSION,
        "run_id": s.run_id,
        "raw_request": s.raw_request,
        "revision": s.revision,
        "stage": s.stage.value,
        "requirements": (
            None if s.requirements is None else _requirements(s.requirements)
        ),
        "candidates": [
            {
                "angle": {
                    "key": c.angle.key,
                    "title": c.angle.title,
                    "description": c.angle.description,
                },
                "itinerary": _itinerary(c.itinerary),
                "facts": _facts(c.facts),
                "status": c.status.value,
                "detail": c.detail,
            }
            for c in s.candidates
        ],
        "chosen_key": s.chosen_key,
        "trip_timezone": s.trip_timezone,
        "seeds": {k: _itinerary(v) for k, v in s.seeds.items()},
        "issues": [_issue(i) for i in s.issues],
    }


def decode_state(raw: dict) -> TripState:
    version = raw.get("format_version")
    if version is None:
        raise UnsupportedVersion(
            "state.json 缺少 format_version 字段；不是可识别的 state.json"
        )
    if version > FORMAT_VERSION:
        raise UnsupportedVersion(
            f"state.json 版本 {version} 高于本工具支持的 {FORMAT_VERSION}；"
            "请升级 tripplan，而不是用旧代码去解析新结构"
        )
    while version < FORMAT_VERSION:
        migrate = MIGRATIONS.get(version)
        if migrate is None:
            raise UnsupportedVersion(f"缺少从版本 {version} 升级的迁移函数")
        raw = migrate(raw)
        new_version = raw["format_version"]
        if new_version <= version:
            # 迁移函数必须推进版本号；否则这个 while 循环会原地死转。
            raise UnsupportedVersion(
                f"迁移函数未能推进版本号：{version} -> {new_version}"
            )
        version = new_version

    s = TripState(
        run_id=raw["run_id"],
        raw_request=raw["raw_request"],
        revision=raw["revision"],
        stage=Stage(raw["stage"]),
    )
    s.requirements = (
        None if raw["requirements"] is None else _un_requirements(raw["requirements"])
    )
    s.candidates = [
        CandidateSlot(
            angle=Angle(**c["angle"]),
            itinerary=_un_itinerary(c["itinerary"]),
            facts=_un_facts(c["facts"]),
            status=SlotStatus(c["status"]),
            detail=c["detail"],
        )
        for c in raw["candidates"]
    ]
    s.chosen_key = raw["chosen_key"]
    s.trip_timezone = raw["trip_timezone"]
    s.seeds = {k: _un_itinerary(v) for k, v in raw["seeds"].items()}
    s.issues = [_un_issue(i) for i in raw["issues"]]
    return s


def dumps(state: TripState) -> str:
    return json.dumps(encode_state(state), ensure_ascii=False, indent=2, sort_keys=True)


def loads(text: str) -> TripState:
    return decode_state(json.loads(text))
