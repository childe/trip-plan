from datetime import date
from decimal import Decimal

from tripplan.models.common import Confidence, Field, Money, Origin
from tripplan.models.facts import Gap, GapKind
from tripplan.models.issue import Issue, Severity, Source
from tripplan.models.itinerary import Category
from tripplan.models.requirements import (
    Basis,
    BudgetSpec,
    CostKind,
    Requirements,
)
from tripplan.render.itinerary_md import render_itinerary_md

D1 = date(2026, 10, 1)


def _itin(mk):
    return mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺"),
                    mk.act(
                        "d1a2",
                        "d1",
                        "12:00",
                        "13:00",
                        query="某食堂",
                        category=Category.MEAL,
                        cost=Money(Decimal("1500"), "JPY", Confidence.ESTIMATED, "llm"),
                    ),
                ],
            )
        ]
    )


def test_renders_days_and_activities_in_order(mk):
    out = render_itinerary_md(_itin(mk), mk.facts(), mk.reqs())
    assert out.index("清水寺") < out.index("某食堂")
    assert "09:00" in out and "2026-10-01" in out


def test_inserts_transit_legs_between_activities(mk):
    facts = mk.facts(routes=[mk.route("d1", "d1a1", "d1a2", 40)])
    out = render_itinerary_md(_itin(mk), facts, mk.reqs())
    assert "40" in out
    assert out.index("清水寺") < out.index("40") < out.index("某食堂")


def test_marks_unverified_transit_instead_of_leaving_a_blank(mk):
    facts = mk.facts(gaps=[mk.gap(GapKind.ROUTE_UNAVAILABLE, "d1a1->d1a2", "高德限流")])
    out = render_itinerary_md(_itin(mk), facts, mk.reqs())
    assert "未能核实" in out


def test_shows_layered_budget_ledger(mk):
    reqs = mk.reqs(
        budget=Field(
            BudgetSpec(
                Decimal("15000"), "JPY", Basis.TOTAL, frozenset({CostKind.MEAL})
            ),
            Origin.USER,
        )
    )
    out = render_itinerary_md(_itin(mk), mk.facts(), reqs)
    assert "估算" in out
    assert "未知" in out  # 清水寺 cost 为 None
    assert "1500" in out


def test_estimated_amounts_are_visually_distinct_from_verified(mk):
    reqs = mk.reqs(
        budget=Field(
            BudgetSpec(
                Decimal("15000"), "JPY", Basis.TOTAL, frozenset({CostKind.MEAL})
            ),
            Origin.USER,
        )
    )
    out = render_itinerary_md(_itin(mk), mk.facts(), reqs)
    assert "已核实" in out and "估算" in out


def test_lists_outstanding_issues(mk):
    itin = _itin(mk)
    itin.issues = [Issue(Severity.WARNING, Source.RULE, "R8", "没安排晚餐")]
    out = render_itinerary_md(itin, mk.facts(), mk.reqs())
    assert "没安排晚餐" in out


def test_lists_unparseable_content_dropped_during_parsing(mk):
    """UNPARSEABLE_DAY / UNPARSEABLE_CRITIQUE 是解析阶段丢内容留下的普通
    WARNING（见 agents/steps.py 的 _parse_itinerary 与 _run_critic），用生产
    代码里真实会出现的字面量构造，确认它们和其他 issue 一样正常显示，不需要
    特殊分支。"""
    itin = _itin(mk)
    itin.issues = [
        Issue(
            severity=Severity.WARNING,
            source=Source.RULE,
            code="UNPARSEABLE_DAY",
            message="原始第 2 天解析失败，已跳过：'date' 字段缺失",
            where=None,
        ),
        Issue(
            severity=Severity.WARNING,
            source=Source.RULE,
            code="UNPARSEABLE_CRITIQUE",
            message="issues 解析失败，整份点评无法使用：not a list",
            where=None,
        ),
    ]
    out = render_itinerary_md(itin, mk.facts(), mk.reqs())
    assert "原始第 2 天解析失败，已跳过：'date' 字段缺失" in out
    assert "issues 解析失败，整份点评无法使用：not a list" in out


def test_lists_unverified_facts_honestly(mk):
    facts = mk.facts(gaps=[mk.gap(GapKind.POI_NOT_FOUND, "d1a1", "查不到")])
    out = render_itinerary_md(_itin(mk), facts, mk.reqs())
    assert "未核实" in out or "未能核实" in out


def test_output_is_stable_across_repeated_renders(mk):
    args = (_itin(mk), mk.facts(), mk.reqs())
    assert render_itinerary_md(*args) == render_itinerary_md(*args)


# ---------- 最终评审 I4：没有预算时不能声称「与预算币种不一致」 ----------


def _mixed_currency_itin(mk):
    return mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act(
                        "d1a1",
                        "d1",
                        "09:00",
                        "10:00",
                        query="酒店",
                        cost=Money(Decimal("10000"), "CNY", Confidence.VERIFIED, "x"),
                    ),
                    mk.act(
                        "d1a2",
                        "d1",
                        "12:00",
                        "13:00",
                        query="拉面",
                        category=Category.MEAL,
                        cost=Money(Decimal("1500"), "JPY", Confidence.ESTIMATED, "llm"),
                    ),
                ],
            )
        ]
    )


def _jpy_budget():
    return Field(
        BudgetSpec(Decimal("15000"), "JPY", Basis.TOTAL, frozenset({CostKind.MEAL})),
        Origin.USER,
    )


def test_mismatch_without_a_budget_does_not_invent_one(mk):
    """用户从未填过预算时，led.currency 只是从行程第一笔花费推断出来的，
    不是任何人给过的基准。说"与预算币种不一致"等于凭空编造一个用户没做过
    的决定 —— rules.py 在 Task 10 就为这句话改过，渲染层当时没跟上。"""
    out = render_itinerary_md(_mixed_currency_itin(mk), mk.facts(), mk.reqs())
    assert "预算" not in out
    assert "行程内花费存在不同币种" in out
    assert "未计入" in out  # 提示本身不能一起消失


def test_mismatch_with_a_budget_still_says_it_is_the_budget_currency(mk):
    """反证：真有预算时那句话是对的，不能因为修 I4 把它一起删掉。"""
    out = render_itinerary_md(
        _mixed_currency_itin(mk), mk.facts(), mk.reqs(budget=_jpy_budget())
    )
    assert "与预算币种不一致" in out


def test_mismatch_wording_matches_the_rule_layer_verdict(mk):
    """同一份文档里，规则给的话与账单给的话不能互相打架：R6 说「行程内
    花费存在不同币种」，账单行却说「与预算不一致」，读者无从判断到底有没有
    预算这回事。"""
    from tripplan.validation.rules import rule_06_budget

    itin, reqs = _mixed_currency_itin(mk), mk.reqs()
    issues = rule_06_budget(itin, reqs, mk.facts())
    out = render_itinerary_md(itin, mk.facts(), reqs)
    rule_msg = next(i.message for i in issues if "币种" in i.message)
    assert ("预算" in rule_msg) == ("预算" in out)
