"""确定性规则。纯函数：只读 Itinerary / Requirements / FactSnapshot。

本模块禁止 import 任何 provider 或网络库。触网的部分在 resolver 里，
规则只消费它产出的快照。依赖缺失时降级为 WARNING 并说明原因——
不按 0 处理，也不假装通过。
"""

from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from tripplan.models.facts import FactSnapshot, GapKind
from tripplan.models.issue import ActivityRef, DayRef, Issue, Severity, Source
from tripplan.models.itinerary import Itinerary
from tripplan.models.requirements import Requirements

#: 实测耗时之上再留 20%——换乘走错口、等信号、找入口都在这里面。
TRANSIT_BUFFER = Decimal("1.2")


def _issue(sev: Severity, code: str, message: str, where=None) -> Issue:
    return Issue(
        severity=sev, source=Source.RULE, code=code, message=message, where=where
    )


def _minutes(t) -> int:
    return t.hour * 60 + t.minute


def rule_01_no_overlap(itin, reqs, facts) -> list[Issue]:
    """同日活动必须时间递增且互不重叠。"""
    issues: list[Issue] = []
    for day in itin.days:
        prev_end = None
        for act in day.activities:
            if _minutes(act.end) <= _minutes(act.start):
                issues.append(
                    _issue(
                        Severity.BLOCKING,
                        "R1",
                        f"{act.poi_query} 的结束时间不晚于开始时间"
                        f"（{act.start:%H:%M}–{act.end:%H:%M}）",
                        ActivityRef(day.id, act.id),
                    )
                )
            elif prev_end is not None and _minutes(act.start) < prev_end:
                issues.append(
                    _issue(
                        Severity.BLOCKING,
                        "R1",
                        f"{act.poi_query} 与上一项时间重叠"
                        f"（{act.start:%H:%M} 早于上一项结束）",
                        ActivityRef(day.id, act.id),
                    )
                )
            prev_end = max(prev_end or 0, _minutes(act.end))
    return issues


def rule_02_transit_gap(itin, reqs, facts) -> list[Issue]:
    """相邻活动之间必须留够实测通勤时间 × TRANSIT_BUFFER。"""
    issues: list[Issue] = []
    for day in itin.days:
        for prev, nxt in zip(day.activities, day.activities[1:]):
            gap = _minutes(nxt.start) - _minutes(prev.end)
            route = facts.route(day.id, prev.id, nxt.id)
            if route is None:
                # 快照里没有这一段：如实降级。gaps 里若有对应记录就带上原因。
                subject = f"{prev.id}->{nxt.id}"
                why = next(
                    (
                        g.detail
                        for g in facts.gaps
                        if g.kind is GapKind.ROUTE_UNAVAILABLE and g.subject == subject
                    ),
                    "",
                )
                suffix = f"（{why}）" if why else ""
                issues.append(
                    _issue(
                        Severity.WARNING,
                        "R2",
                        f"{prev.poi_query} 到 {nxt.poi_query} 的通勤耗时未能核实"
                        f"{suffix}，当前只留了 {gap} 分钟",
                        ActivityRef(day.id, nxt.id),
                    )
                )
                continue
            needed = int(Decimal(route.duration_min) * TRANSIT_BUFFER)
            if gap < needed:
                issues.append(
                    _issue(
                        Severity.BLOCKING,
                        "R2",
                        f"{prev.poi_query} 到 {nxt.poi_query} 实测需 "
                        f"{route.duration_min} 分钟（含缓冲 {needed}），"
                        f"但只留了 {gap} 分钟",
                        ActivityRef(day.id, nxt.id),
                    )
                )
    return issues


def rule_03_date_coverage(itin, reqs, facts) -> list[Issue]:
    """日期必须与需求完全对齐；抵离时刻已知时，首末日不得越界。"""
    issues: list[Issue] = []
    rng = reqs.dates.value
    if rng is None:
        return issues

    wanted = {rng.start + timedelta(days=i) for i in range(rng.days)}
    got = {d.date for d in itin.days}
    for missing in sorted(wanted - got):
        issues.append(
            _issue(Severity.BLOCKING, "R3", f"缺少 {missing.isoformat()} 的安排")
        )
    for extra in sorted(got - wanted):
        issues.append(
            _issue(Severity.BLOCKING, "R3", f"{extra.isoformat()} 不在行程日期范围内")
        )

    tz = ZoneInfo(facts.trip_timezone)
    checked_transfer = False

    arrival = reqs.arrival.value
    if arrival is not None:
        checked_transfer = True
        local = arrival.at.astimezone(tz)
        for day in itin.days:
            if day.date != local.date():
                continue
            for act in day.activities:
                if _minutes(act.start) < _minutes(local.time()):
                    issues.append(
                        _issue(
                            Severity.BLOCKING,
                            "R3",
                            f"{act.poi_query} 安排在 {act.start:%H:%M}，"
                            f"早于 {local:%H:%M} 的抵达时刻",
                            ActivityRef(day.id, act.id),
                        )
                    )

    departure = reqs.departure.value
    if departure is not None:
        checked_transfer = True
        local = departure.at.astimezone(tz)
        for day in itin.days:
            if day.date != local.date():
                continue
            for act in day.activities:
                if _minutes(act.end) > _minutes(local.time()):
                    issues.append(
                        _issue(
                            Severity.BLOCKING,
                            "R3",
                            f"{act.poi_query} 到 {act.end:%H:%M} 才结束，"
                            f"晚于 {local:%H:%M} 的离开时刻",
                            ActivityRef(day.id, act.id),
                        )
                    )

    if not checked_transfer:
        # 只有首末日真排了活动，「按整天计」才可能算多——空的日子没有可担
        # 心的东西。
        boundary_dates = {min(wanted), max(wanted)} if wanted else set()
        boundary_has_activities = any(
            day.activities for day in itin.days if day.date in boundary_dates
        )
        if boundary_has_activities:
            issues.append(
                _issue(
                    Severity.WARNING,
                    "R3",
                    "未提供抵离时间，首末日按整天计——实际可用时间可能更短",
                )
            )
    return issues


def _constraint_issues(itin, facts, queries, code, want_present) -> list[Issue]:
    """规则 4 与 5 的公共骨架：方向相反，其余完全一致。"""
    issues: list[Issue] = []
    scheduled = facts.resolved_poi_ids()
    for query in queries or []:
        poi_id = facts.constraint_poi_id(query)
        if poi_id is None:
            issues.append(
                _issue(
                    Severity.WARNING, code, f"无法核实「{query}」——该地点未能唯一解析"
                )
            )
            continue
        present = poi_id in scheduled
        if present is not want_present:
            msg = (
                f"必去的「{query}」没有出现在行程里"
                if want_present
                else f"要求避开的「{query}」出现在了行程里"
            )
            issues.append(_issue(Severity.BLOCKING, code, msg))
    return issues


def rule_04_must_visit(itin, reqs, facts) -> list[Issue]:
    return _constraint_issues(itin, facts, reqs.must_visit.value, "R4", True)


def rule_05_avoid(itin, reqs, facts) -> list[Issue]:
    return _constraint_issues(itin, facts, reqs.avoid.value, "R5", False)
