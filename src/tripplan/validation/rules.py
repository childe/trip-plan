"""确定性规则。纯函数：只读 Itinerary / Requirements / FactSnapshot。

本模块禁止 import 任何 provider 或网络库。触网的部分在 resolver 里，
规则只消费它产出的快照。依赖缺失时降级为 WARNING 并说明原因——
不按 0 处理，也不假装通过。
"""

from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from zoneinfo import ZoneInfo

from tripplan.models.facts import FactSnapshot, GapKind
from tripplan.models.issue import ActivityRef, DayRef, Issue, Severity, Source
from tripplan.models.itinerary import Category, Itinerary
from tripplan.models.requirements import Pace, Requirements
from tripplan.validation.budget import build_ledger

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
            # 不在这里就地取整——40 分钟这种整十数乘 1.2 恰好还是整数，
            # 会掩盖截断问题；13 分钟乘 1.2 是 15.6，截断成 15 就会把
            # 15 分钟的间隙误判为够用。比较必须用没有取整的 Decimal。
            needed = Decimal(route.duration_min) * TRANSIT_BUFFER
            if Decimal(gap) < needed:
                # 展示用的整数只能向上取整，不能向下——否则文案会声称
                # 比实际比较用的门槛更松，跟真正做的判断对不上。
                needed_display = int(needed.to_integral_value(rounding=ROUND_CEILING))
                issues.append(
                    _issue(
                        Severity.BLOCKING,
                        "R2",
                        f"{prev.poi_query} 到 {nxt.poi_query} 实测需 "
                        f"{route.duration_min} 分钟（含缓冲 {needed_display}），"
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

    arrival = reqs.arrival.value
    if arrival is not None:
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

    # 抵达、离开是两件独立的事——任何一侧缺失都必须单独说清楚，不能因为
    # 另一侧已知就用一个共享标记把这一侧的「未核实」悄悄吞掉。
    first_date = min(wanted) if wanted else None
    last_date = max(wanted) if wanted else None

    def _has_activities(d):
        day = next((day for day in itin.days if day.date == d), None)
        return day is not None and bool(day.activities)

    # 只有首/末日真排了活动，「按整天计」才可能算多——空的日子没有可担
    # 心的东西。
    arrival_missing = (
        arrival is None and first_date is not None and _has_activities(first_date)
    )
    departure_missing = (
        departure is None and last_date is not None and _has_activities(last_date)
    )

    if first_date is not None and first_date == last_date:
        # 单日行程：首末是同一天，缺失哪一侧就在一条消息里点名，不要为
        # 同一天重复发两条几乎一样的提示。
        if arrival_missing or departure_missing:
            if arrival is None and departure is None:
                sides = "抵达和离开时间"
            elif arrival is None:
                sides = "抵达时间"
            else:
                sides = "离开时间"
            issues.append(
                _issue(
                    Severity.WARNING,
                    "R3",
                    f"未提供{sides}，{first_date.isoformat()} 按整天计——"
                    "实际可用时间可能更短",
                )
            )
    else:
        if arrival_missing:
            issues.append(
                _issue(
                    Severity.WARNING,
                    "R3",
                    f"未提供抵达时间，第一天（{first_date.isoformat()}）"
                    "按整天计——实际可用时间可能更短",
                )
            )
        if departure_missing:
            issues.append(
                _issue(
                    Severity.WARNING,
                    "R3",
                    f"未提供离开时间，最后一天（{last_date.isoformat()}）"
                    "按整天计——实际可用时间可能更短",
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


# ---------- 规则 6：预算 ----------


def rule_06_budget(itin, reqs, facts) -> list[Issue]:
    """输出分层账单式的判断，而不是拿模型自报的数字去 BLOCK 模型自己的方案。

    只有覆盖完整（无未知项）且全部 VERIFIED 时超支才升为 BLOCKING。
    这个条件在 v1 基本不会满足，接入真实票价 API 后自然生效。
    """
    issues: list[Issue] = []
    led = build_ledger(itin, reqs)

    if led.currency_mismatch:
        # 有预算时，币种基准来自用户填的预算；没有预算时，led.currency 只是
        # 从行程里第一笔有价格的花费推断出来的——不能把「行程内部币种不一
        # 致」说成「和一个用户从未填过的预算冲突」。
        if reqs.budget.value is not None:
            mismatch_msg = f"部分花费的币种与预算（{led.currency}）不一致，未计入合计"
        else:
            mismatch_msg = (
                f"行程内花费存在不同币种，合计仅按其中一种（{led.currency}）"
                "计算，其余未计入"
            )
        issues.append(_issue(Severity.WARNING, "R6", mismatch_msg))

    if not led.over_budget:
        return issues

    detail = (
        f"已核实 {led.verified}（{led.verified_count} 项）"
        f"／估算 {led.estimated}（{led.estimated_count} 项）"
        f"／未知 {led.unknown_count} 项，预算 {led.budget_limit}"
    )
    if led.complete and led.all_verified:
        issues.append(_issue(Severity.BLOCKING, "R6", f"超出预算：{detail}"))
    else:
        issues.append(
            _issue(
                Severity.WARNING,
                "R6",
                f"按当前估算可能超出预算：{detail}（金额未全部核实，仅供参考）",
            )
        )
    return issues


# ---------- 规则 7：节奏 ----------


class PaceLimit:
    def __init__(self, max_activities: int, max_out_minutes: int) -> None:
        self.max_activities = max_activities
        self.max_out_minutes = max_out_minutes


PACE_LIMITS: dict[Pace, PaceLimit] = {
    Pace.RELAXED: PaceLimit(max_activities=4, max_out_minutes=8 * 60),
    Pace.NORMAL: PaceLimit(max_activities=6, max_out_minutes=10 * 60),
    Pace.PACKED: PaceLimit(max_activities=8, max_out_minutes=12 * 60),
}


def rule_07_pace(itin, reqs, facts) -> list[Issue]:
    pace = reqs.pace.value or Pace.NORMAL
    limit = PACE_LIMITS[pace]
    issues: list[Issue] = []
    for day in itin.days:
        if not day.activities:
            continue
        count = len(day.activities)
        out = _minutes(day.activities[-1].end) - _minutes(day.activities[0].start)
        if count > limit.max_activities:
            issues.append(
                _issue(
                    Severity.WARNING,
                    "R7",
                    f"{day.date.isoformat()} 排了 {count} 项，"
                    f"超过 {pace.value} 节奏建议的 {limit.max_activities} 项",
                    DayRef(day.id),
                )
            )
        # 项数超标和在外时长超标是两个不同的问题（对策分别是砍活动/压缩行程），
        # 各自独立判断——不能用 elif 让第二条命中的事实被吞掉。
        if out > limit.max_out_minutes:
            issues.append(
                _issue(
                    Severity.WARNING,
                    "R7",
                    f"{day.date.isoformat()} 在外 {out // 60} 小时，"
                    f"超过 {pace.value} 节奏建议的 "
                    f"{limit.max_out_minutes // 60} 小时",
                    DayRef(day.id),
                )
            )
    return issues


# ---------- 规则 8：三餐 ----------

MEAL_WINDOWS = (("午餐", 11 * 60, 14 * 60 + 30), ("晚餐", 17 * 60, 21 * 60))


def rule_08_meals(itin, reqs, facts) -> list[Issue]:
    issues: list[Issue] = []
    for day in itin.days:
        if not day.activities:
            continue
        for label, lo, hi in MEAL_WINDOWS:
            ok = any(
                a.category is Category.MEAL and lo <= _minutes(a.start) <= hi
                for a in day.activities
            )
            if not ok:
                issues.append(
                    _issue(
                        Severity.WARNING,
                        "R8",
                        f"{day.date.isoformat()} 没有安排{label}",
                        DayRef(day.id),
                    )
                )
    return issues


# ---------- 规则 9：营业时间 ----------


def _parse_hours(text: str) -> tuple[int, int] | None:
    try:
        lo, hi = text.split("-")
        h1, m1 = map(int, lo.strip().split(":"))
        h2, m2 = map(int, hi.strip().split(":"))
        return h1 * 60 + m1, h2 * 60 + m2
    except (ValueError, AttributeError):
        return None


def rule_09_opening_hours(itin, reqs, facts) -> list[Issue]:
    """v1 没有权威营业时间数据源，因此**永远只给 WARNING**。

    接入真实 API 后才能升为 BLOCKING——不假装它可靠，比悄悄放过去强。
    """
    issues: list[Issue] = []
    for day in itin.days:
        for act in day.activities:
            res = facts.poi_by_activity.get(act.id)
            hours = getattr(getattr(res, "fact", None), "opening_hours", None)
            window = _parse_hours(hours) if hours else None
            if window is None:
                continue
            lo, hi = window
            if _minutes(act.start) < lo or _minutes(act.end) > hi:
                issues.append(
                    _issue(
                        Severity.WARNING,
                        "R9",
                        f"{act.poi_query} 的安排（{act.start:%H:%M}–{act.end:%H:%M}）"
                        f"可能不在营业时间 {hours} 内（该数据未核实）",
                        ActivityRef(day.id, act.id),
                    )
                )
    return issues


# ---------- 汇总 ----------

ALL_RULES = (
    rule_01_no_overlap,
    rule_02_transit_gap,
    rule_03_date_coverage,
    rule_04_must_visit,
    rule_05_avoid,
    rule_06_budget,
    rule_07_pace,
    rule_08_meals,
    rule_09_opening_hours,
)


def run_rule_checks(
    itin: Itinerary, reqs: Requirements, facts: FactSnapshot
) -> list[Issue]:
    issues: list[Issue] = []
    for rule in ALL_RULES:
        issues.extend(rule(itin, reqs, facts))
    return issues
