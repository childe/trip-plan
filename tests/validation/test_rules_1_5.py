from datetime import date, datetime, timedelta, timezone

from tripplan.models.facts import Ambiguous, GapKind, NotFound
from tripplan.models.issue import Severity
from tripplan.validation.rules import (
    rule_01_no_overlap,
    rule_02_transit_gap,
    rule_03_date_coverage,
    rule_04_must_visit,
    rule_05_avoid,
)

JST = timezone(timedelta(hours=9))
OFFSET8 = timezone(timedelta(hours=8))
D1 = date(2026, 10, 1)
D2 = date(2026, 10, 2)
D3 = date(2026, 10, 3)


def _codes(issues):
    return [i.code for i in issues]


def _sev(issues):
    return [i.severity for i in issues]


# ---------- 规则 1：同日活动不重叠、时间递增 ----------


def test_r1_passes_on_sequential_activities(mk):
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00"),
                    mk.act("d1a2", "d1", "13:00", "14:00"),
                ],
            )
        ]
    )
    assert rule_01_no_overlap(itin, mk.reqs(), mk.facts()) == []


def test_r1_flags_overlap(mk):
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "12:00"),
                    mk.act("d1a2", "d1", "11:00", "13:00"),
                ],
            )
        ]
    )
    issues = rule_01_no_overlap(itin, mk.reqs(), mk.facts())
    assert _codes(issues) == ["R1"]
    assert _sev(issues) == [Severity.BLOCKING]
    assert issues[0].where.activity_id == "d1a2"


def test_r1_flags_activity_ending_before_it_starts(mk):
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "14:00", "09:00")])])
    issues = rule_01_no_overlap(itin, mk.reqs(), mk.facts())
    assert _codes(issues) == ["R1"]


def test_r1_does_not_compare_across_days(mk):
    itin = mk.itin(
        [
            mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "23:00")]),
            mk.day("d2", D2, [mk.act("d2a1", "d2", "09:00", "10:00")]),
        ]
    )
    assert rule_01_no_overlap(itin, mk.reqs(start=D1, end=D2), mk.facts()) == []


def test_r1_carries_running_max_across_non_adjacent_activities(mk):
    """B 与 A 重叠会被抓到；C 虽然晚于 B 结束，却仍落在 A 的结束时间之内——
    如果规则退化成只跟"上一项"比较（而不是跟运行中的最大结束时间比较），
    这一条就会被放过。"""
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "15:00"),
                    mk.act("d1a2", "d1", "09:30", "10:00"),
                    mk.act("d1a3", "d1", "10:30", "11:00"),
                ],
            )
        ]
    )
    issues = rule_01_no_overlap(itin, mk.reqs(), mk.facts())
    assert _codes(issues) == ["R1", "R1"]
    assert [i.where.activity_id for i in issues] == ["d1a2", "d1a3"]


# ---------- 规则 2：通勤间隙 ----------


def test_r2_passes_when_gap_covers_duration_with_buffer(mk):
    """间隙 120 分钟 ≥ 40 × 1.2 = 48。"""
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00"),
                    mk.act("d1a2", "d1", "13:00", "14:00"),
                ],
            )
        ]
    )
    facts = mk.facts(routes=[mk.route("d1", "d1a1", "d1a2", 40)])
    assert rule_02_transit_gap(itin, mk.reqs(), facts) == []


def test_r2_blocks_when_gap_too_small(mk):
    """间隙 30 分钟 < 40 × 1.2 = 48。"""
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00"),
                    mk.act("d1a2", "d1", "11:30", "12:30"),
                ],
            )
        ]
    )
    facts = mk.facts(routes=[mk.route("d1", "d1a1", "d1a2", 40)])
    issues = rule_02_transit_gap(itin, mk.reqs(), facts)
    assert _codes(issues) == ["R2"]
    assert _sev(issues) == [Severity.BLOCKING]
    assert "40" in issues[0].message and "30" in issues[0].message


def test_r2_buffer_boundary_is_inclusive(mk):
    """间隙恰好等于 40 × 1.2 = 48 分钟时通过。"""
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00"),
                    mk.act("d1a2", "d1", "11:48", "12:30"),
                ],
            )
        ]
    )
    facts = mk.facts(routes=[mk.route("d1", "d1a1", "d1a2", 40)])
    assert rule_02_transit_gap(itin, mk.reqs(), facts) == []


def test_r2_blocks_on_fractional_buffer_without_truncation(mk):
    """13 × 1.2 = 15.6 分钟；15 分钟不够——如果拿 int() 把 15.6 截断成 15，
    这一条就会被漏判（只有缓冲后恰好是整数的耗时才不受影响，40 分钟就是
    这样凑巧躲过了原来的测试）。"""
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00"),
                    mk.act("d1a2", "d1", "11:15", "12:00"),
                ],
            )
        ]
    )
    facts = mk.facts(routes=[mk.route("d1", "d1a1", "d1a2", 13)])
    issues = rule_02_transit_gap(itin, mk.reqs(), facts)
    assert _codes(issues) == ["R2"]
    assert _sev(issues) == [Severity.BLOCKING]


def test_r2_degrades_to_warning_when_route_unavailable(mk):
    """查不到就如实说查不到——不按 0 处理，也不假装通过。"""
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00"),
                    mk.act("d1a2", "d1", "11:05", "12:00"),
                ],
            )
        ]
    )
    facts = mk.facts(gaps=[mk.gap(GapKind.ROUTE_UNAVAILABLE, "d1a1->d1a2")])
    issues = rule_02_transit_gap(itin, mk.reqs(), facts)
    assert _sev(issues) == [Severity.WARNING]
    assert "未能核实" in issues[0].message


# ---------- 规则 3：日期覆盖与抵离占用 ----------


def test_r3_passes_on_exact_coverage(mk):
    itin = mk.itin([mk.day("d1", D1, []), mk.day("d2", D2, [])])
    assert rule_03_date_coverage(itin, mk.reqs(start=D1, end=D2), mk.facts()) == []


def test_r3_blocks_on_missing_day(mk):
    itin = mk.itin([mk.day("d1", D1, [])])
    issues = rule_03_date_coverage(itin, mk.reqs(start=D1, end=D2), mk.facts())
    assert _codes(issues) == ["R3"]
    assert _sev(issues) == [Severity.BLOCKING]
    assert "2026-10-02" in issues[0].message


def test_r3_blocks_on_extra_day(mk):
    itin = mk.itin([mk.day("d1", D1, []), mk.day("d2", D2, [])])
    issues = rule_03_date_coverage(itin, mk.reqs(start=D1, end=D1), mk.facts())
    assert _sev(issues) == [Severity.BLOCKING]


def test_r3_blocks_activity_before_arrival(mk):
    """15:00 落地，却排了 09:00 的活动。这是单日行程，departure 没给——
    离开这一侧本来就没法核实，所以除了这条 BLOCKING，还应该有一条
    WARNING 如实说「离开时间未知」，而不是被 arrival 已知这件事悄悄盖过去。"""
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00")])])
    reqs = mk.reqs(start=D1, end=D1, arrival=datetime(2026, 10, 1, 15, 0, tzinfo=JST))
    issues = rule_03_date_coverage(itin, reqs, mk.facts())
    assert _codes(issues) == ["R3", "R3"]
    assert _sev(issues) == [Severity.BLOCKING, Severity.WARNING]
    assert issues[0].where.activity_id == "d1a1"
    assert "离开" in issues[1].message


def test_r3_blocks_activity_after_departure(mk):
    """同理：departure 已知、arrival 未给，除了 BLOCKING 还应该有一条
    WARNING 如实说「抵达时间未知」。"""
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "18:00", "20:00")])])
    reqs = mk.reqs(start=D1, end=D1, departure=datetime(2026, 10, 1, 17, 0, tzinfo=JST))
    issues = rule_03_date_coverage(itin, reqs, mk.facts())
    assert _codes(issues) == ["R3", "R3"]
    assert _sev(issues) == [Severity.BLOCKING, Severity.WARNING]
    assert "抵达" in issues[1].message


def test_r3_warns_when_transfer_times_unknown(mk):
    """原版声称「首末日扣掉抵离占用」，但模型里根本没这个字段。
    现在字段有了；缺失时如实说，不谎称已扣除。"""
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00")])])
    issues = rule_03_date_coverage(itin, mk.reqs(start=D1, end=D1), mk.facts())
    assert _sev(issues) == [Severity.WARNING]
    assert "按整天计" in issues[0].message


def test_r3_warns_departure_side_only_when_arrival_known(mk):
    """三天的行程：已知抵达、未知离开，最后一天排了活动。
    原来的实现用一个共享的 checked_transfer 标记——只要抵达那侧已知，
    离开那侧完全没检查过这件事就被悄悄吞掉了。应该按侧分别判断：
    抵达没问题（没有 BLOCKING），但离开这一侧必须如实说未核实。"""
    itin = mk.itin(
        [
            mk.day("d1", D1, []),
            mk.day("d2", D2, []),
            mk.day("d3", D3, [mk.act("d3a1", "d3", "20:00", "23:00")]),
        ]
    )
    reqs = mk.reqs(start=D1, end=D3, arrival=datetime(2026, 10, 1, 8, 0, tzinfo=JST))
    issues = rule_03_date_coverage(itin, reqs, mk.facts())
    assert _sev(issues) == [Severity.WARNING]
    assert "离开" in issues[0].message


def test_r3_warns_arrival_side_only_when_departure_known(mk):
    """反过来：已知离开、未知抵达，第一天排了活动。"""
    itin = mk.itin(
        [
            mk.day("d1", D1, [mk.act("d1a1", "d1", "06:00", "08:00")]),
            mk.day("d2", D2, []),
            mk.day("d3", D3, []),
        ]
    )
    reqs = mk.reqs(start=D1, end=D3, departure=datetime(2026, 10, 3, 20, 0, tzinfo=JST))
    issues = rule_03_date_coverage(itin, reqs, mk.facts())
    assert _sev(issues) == [Severity.WARNING]
    assert "抵达" in issues[0].message


def test_r3_converts_transfer_time_across_timezones(mk):
    """抵达时刻以 +08:00 记录，行程时区是 Asia/Tokyo（+09:00）——
    2026-10-01T07:30+08:00 换算成当地时间是 08:30，而不是原始的 07:30。
    必须先 astimezone 到行程时区再比较，不能直接拿 wall-clock 数值去比。"""
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "08:00", "09:00")])])
    reqs = mk.reqs(
        start=D1, end=D1, arrival=datetime(2026, 10, 1, 7, 30, tzinfo=OFFSET8)
    )
    issues = rule_03_date_coverage(itin, reqs, mk.facts())
    assert _sev(issues) == [Severity.BLOCKING, Severity.WARNING]
    assert "08:30" in issues[0].message


# ---------- 规则 4 / 5：必去与排除 ----------


def test_r4_passes_when_constraint_poi_is_scheduled(mk):
    itin = mk.itin(
        [mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺")])]
    )
    facts = mk.facts(
        poi_by_activity={"d1a1": mk.resolved("B001")},
        constraint_pois={"清水寺": mk.resolved("B001")},
    )
    assert rule_04_must_visit(itin, mk.reqs(must_visit=["清水寺"]), facts) == []


def test_r4_matches_by_poi_id_not_string(mk):
    """「清水寺」与「清水寺（京都）」是同一个地方，字符串比不出来。"""
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺（京都）")],
            )
        ]
    )
    facts = mk.facts(
        poi_by_activity={"d1a1": mk.resolved("B001")},
        constraint_pois={"清水寺": mk.resolved("B001")},
    )
    assert rule_04_must_visit(itin, mk.reqs(must_visit=["清水寺"]), facts) == []


def test_r4_blocks_when_required_poi_absent(mk):
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00")])])
    facts = mk.facts(
        poi_by_activity={"d1a1": mk.resolved("B999")},
        constraint_pois={"清水寺": mk.resolved("B001")},
    )
    issues = rule_04_must_visit(itin, mk.reqs(must_visit=["清水寺"]), facts)
    assert _codes(issues) == ["R4"]
    assert _sev(issues) == [Severity.BLOCKING]


def test_r4_degrades_when_constraint_is_ambiguous(mk):
    """约束侧解析不唯一时不能拿猜的 id 去判 BLOCKING——
    #4 是 BLOCKING 级，判错会驱动 planner 反复修改一个本来正确的行程。"""
    facts = mk.facts(
        constraint_pois={"某某寺": Ambiguous([mk.poi("B1"), mk.poi("B2")])}
    )
    itin = mk.itin([mk.day("d1", D1, [])])
    issues = rule_04_must_visit(itin, mk.reqs(must_visit=["某某寺"]), facts)
    assert _sev(issues) == [Severity.WARNING]
    assert "无法核实" in issues[0].message


def test_r4_degrades_when_constraint_not_found(mk):
    facts = mk.facts(constraint_pois={"不存在": NotFound("不存在")})
    itin = mk.itin([mk.day("d1", D1, [])])
    issues = rule_04_must_visit(itin, mk.reqs(must_visit=["不存在"]), facts)
    assert _sev(issues) == [Severity.WARNING]


def test_r5_blocks_when_avoided_poi_is_scheduled(mk):
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00")])])
    facts = mk.facts(
        poi_by_activity={"d1a1": mk.resolved("B001")},
        constraint_pois={"金阁寺": mk.resolved("B001")},
    )
    issues = rule_05_avoid(itin, mk.reqs(avoid=["金阁寺"]), facts)
    assert _codes(issues) == ["R5"]
    assert _sev(issues) == [Severity.BLOCKING]


def test_r5_passes_when_avoided_poi_absent(mk):
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "11:00")])])
    facts = mk.facts(
        poi_by_activity={"d1a1": mk.resolved("B999")},
        constraint_pois={"金阁寺": mk.resolved("B001")},
    )
    assert rule_05_avoid(itin, mk.reqs(avoid=["金阁寺"]), facts) == []


def test_r5_degrades_when_constraint_unresolved(mk):
    facts = mk.facts(constraint_pois={"某寺": NotFound("某寺")})
    itin = mk.itin([mk.day("d1", D1, [])])
    assert _sev(rule_05_avoid(itin, mk.reqs(avoid=["某寺"]), facts)) == [
        Severity.WARNING
    ]


def test_rules_module_imports_nothing_networky():
    """规则必须是纯函数——测试直接盯住 import。"""
    import ast
    import pathlib

    import tripplan.validation.rules as rules

    src = pathlib.Path(rules.__file__).read_text(encoding="utf-8")
    imported = {
        n.module or ""
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.ImportFrom)
    } | {
        a.name
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Import)
        for a in n.names
    }
    forbidden = {"httpx", "requests", "urllib", "urllib.request", "socket"}
    assert not (imported & forbidden)
    assert not any(m.startswith("tripplan.providers") for m in imported)
