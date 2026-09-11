from datetime import date
from decimal import Decimal

from tripplan.models.common import Confidence, Field, Money, Origin
from tripplan.models.facts import PoiFact, Resolved
from tripplan.models.issue import Severity
from tripplan.models.itinerary import Category
from tripplan.models.requirements import Basis, BudgetSpec, CostKind, Pace
from tripplan.validation.rules import (
    run_rule_checks,
    rule_06_budget,
    rule_07_pace,
    rule_08_meals,
    rule_09_opening_hours,
)

D1 = date(2026, 10, 1)


def _codes(issues):
    return [i.code for i in issues]


def _sev(issues):
    return [i.severity for i in issues]


def _budget(mk, amount="1000"):
    return mk.reqs(
        budget=Field(
            BudgetSpec(
                Decimal(amount), "CNY", Basis.TOTAL, frozenset({CostKind.TICKET})
            ),
            Origin.USER,
        )
    )


def _cost(v, conf=Confidence.ESTIMATED):
    return Money(Decimal(v), "CNY", conf, "llm")


# ---------- 规则 6：预算 ----------


def test_r6_estimated_overspend_is_warning_not_blocking(mk):
    """v1 票价来自模型知识——拿模型自己报的数字去 BLOCK 模型自己的方案不成立。"""
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00", cost=_cost("5000")),
                ],
            )
        ]
    )
    issues = rule_06_budget(itin, _budget(mk), mk.facts())
    assert _codes(issues) == ["R6"]
    assert _sev(issues) == [Severity.WARNING]


def test_r6_blocks_only_when_complete_and_all_verified(mk):
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act(
                        "d1a1",
                        "d1",
                        "09:00",
                        "11:00",
                        cost=_cost("5000", Confidence.VERIFIED),
                    ),
                ],
            )
        ]
    )
    issues = rule_06_budget(itin, _budget(mk), mk.facts())
    assert _sev(issues) == [Severity.BLOCKING]


def test_r6_unknown_cost_keeps_it_a_warning_even_if_verified(mk):
    """有未知项时「合计」本身就不可信，不能据此 BLOCK。"""
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act(
                        "d1a1",
                        "d1",
                        "09:00",
                        "11:00",
                        cost=_cost("5000", Confidence.VERIFIED),
                    ),
                    mk.act("d1a2", "d1", "12:00", "13:00", cost=None),
                ],
            )
        ]
    )
    assert _sev(rule_06_budget(itin, _budget(mk), mk.facts())) == [Severity.WARNING]


def test_r6_silent_when_within_budget(mk):
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00", cost=_cost("100")),
                ],
            )
        ]
    )
    assert rule_06_budget(itin, _budget(mk), mk.facts()) == []


def test_r6_silent_without_a_budget(mk):
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00", cost=_cost("99999")),
                ],
            )
        ]
    )
    assert rule_06_budget(itin, mk.reqs(), mk.facts()) == []


def test_r6_warns_on_currency_mismatch(mk):
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act(
                        "d1a1",
                        "d1",
                        "09:00",
                        "11:00",
                        cost=Money(Decimal("4000"), "JPY", Confidence.ESTIMATED, "llm"),
                    ),
                ],
            )
        ]
    )
    issues = rule_06_budget(itin, _budget(mk), mk.facts())
    assert any("币种" in i.message for i in issues)


# ---------- 规则 7：节奏 ----------


def test_r7_warns_when_relaxed_day_is_overpacked(mk):
    acts = [mk.act(f"d1a{i}", "d1", f"{7 + i}:00", f"{7 + i}:45") for i in range(1, 8)]
    itin = mk.itin([mk.day("d1", D1, acts)])
    reqs = mk.reqs(pace=Field(Pace.RELAXED, Origin.USER))
    issues = rule_07_pace(itin, reqs, mk.facts())
    assert _codes(issues) == ["R7"]
    assert _sev(issues) == [Severity.WARNING]


def test_r7_silent_when_within_pace(mk):
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "10:00", "12:00"),
                    mk.act("d1a2", "d1", "14:00", "16:00"),
                ],
            )
        ]
    )
    reqs = mk.reqs(pace=Field(Pace.RELAXED, Origin.USER))
    assert rule_07_pace(itin, reqs, mk.facts()) == []


def test_r7_packed_allows_more_than_relaxed(mk):
    acts = [mk.act(f"d1a{i}", "d1", f"{7 + i}:00", f"{7 + i}:45") for i in range(1, 7)]
    itin = mk.itin([mk.day("d1", D1, acts)])
    packed = mk.reqs(pace=Field(Pace.PACKED, Origin.USER))
    relaxed = mk.reqs(pace=Field(Pace.RELAXED, Origin.USER))
    assert rule_07_pace(itin, packed, mk.facts()) == []
    assert rule_07_pace(itin, relaxed, mk.facts()) != []


def test_r7_defaults_to_normal_when_pace_unset(mk):
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "09:00", "10:00")])])
    assert rule_07_pace(itin, mk.reqs(), mk.facts()) == []


# ---------- 规则 8：三餐 ----------


def test_r8_warns_when_a_day_has_no_lunch(mk):
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00"),
                    mk.act("d1a2", "d1", "18:00", "19:30", category=Category.MEAL),
                ],
            )
        ]
    )
    issues = rule_08_meals(itin, mk.reqs(), mk.facts())
    assert _codes(issues) == ["R8"]
    assert "午餐" in issues[0].message


def test_r8_silent_when_both_meals_present(mk):
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "12:00", "13:00", category=Category.MEAL),
                    mk.act("d1a2", "d1", "18:30", "20:00", category=Category.MEAL),
                ],
            )
        ]
    )
    assert rule_08_meals(itin, mk.reqs(), mk.facts()) == []


def test_r8_meal_outside_window_does_not_count(mk):
    """凌晨 3 点的「用餐」不能顶替午餐。"""
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "03:00", "04:00", category=Category.MEAL),
                    mk.act("d1a2", "d1", "18:30", "20:00", category=Category.MEAL),
                ],
            )
        ]
    )
    assert "午餐" in rule_08_meals(itin, mk.reqs(), mk.facts())[0].message


# ---------- 规则 9：营业时间 ----------


def test_r9_warns_when_activity_falls_outside_opening_hours(mk):
    poi = PoiFact(
        id="B1",
        name="清水寺",
        coords=mk.poi("B1").coords,
        opening_hours="09:00-17:00",
        ticket=None,
        source="amap",
        fetched_at=mk.poi("B1").fetched_at,
    )
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "18:00", "19:00", query="清水寺"),
                ],
            )
        ]
    )
    facts = mk.facts(poi_by_activity={"d1a1": Resolved(poi)})
    issues = rule_09_opening_hours(itin, mk.reqs(), facts)
    assert _codes(issues) == ["R9"]
    assert _sev(issues) == [Severity.WARNING]  # 数据不可靠，永远不 BLOCK
    assert "未核实" in issues[0].message


def test_r9_silent_when_inside_opening_hours(mk):
    poi = PoiFact(
        id="B1",
        name="清水寺",
        coords=mk.poi("B1").coords,
        opening_hours="09:00-17:00",
        ticket=None,
        source="amap",
        fetched_at=mk.poi("B1").fetched_at,
    )
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "10:00", "11:00", query="清水寺"),
                ],
            )
        ]
    )
    facts = mk.facts(poi_by_activity={"d1a1": Resolved(poi)})
    assert rule_09_opening_hours(itin, mk.reqs(), facts) == []


def test_r9_silent_when_hours_unknown(mk):
    itin = mk.itin([mk.day("d1", D1, [mk.act("d1a1", "d1", "23:00", "23:30")])])
    facts = mk.facts(poi_by_activity={"d1a1": mk.resolved("B1")})
    assert rule_09_opening_hours(itin, mk.reqs(), facts) == []


# ---------- 汇总 ----------


def test_run_rule_checks_aggregates_all_nine(mk):
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "12:00"),
                    mk.act("d1a2", "d1", "11:00", "13:00"),  # R1 重叠
                ],
            )
        ]
    )
    issues = run_rule_checks(itin, mk.reqs(start=D1, end=D1), mk.facts())
    codes = set(_codes(issues))
    assert "R1" in codes
    assert "R3" in codes  # 缺抵离时间的 WARNING
    assert "R8" in codes  # 没有午晚餐


def test_run_rule_checks_returns_empty_on_clean_itinerary(mk):
    from datetime import datetime, timedelta, timezone

    jst = timezone(timedelta(hours=9))
    itin = mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "10:00", "11:30"),
                    mk.act("d1a2", "d1", "12:30", "13:30", category=Category.MEAL),
                    mk.act("d1a3", "d1", "18:30", "20:00", category=Category.MEAL),
                ],
            )
        ]
    )
    facts = mk.facts(
        routes=[
            mk.route("d1", "d1a1", "d1a2", 30),
            mk.route("d1", "d1a2", "d1a3", 30),
        ]
    )
    reqs = mk.reqs(
        start=D1,
        end=D1,
        arrival=datetime(2026, 10, 1, 8, tzinfo=jst),
        departure=datetime(2026, 10, 1, 22, tzinfo=jst),
    )
    assert run_rule_checks(itin, reqs, facts) == []
