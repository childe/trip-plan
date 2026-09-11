import base64
import re
from datetime import date
from decimal import Decimal

from tripplan.models.common import Confidence, Field, Money, Origin
from tripplan.models.facts import GapKind
from tripplan.models.issue import Issue, Severity, Source
from tripplan.models.itinerary import Category
from tripplan.models.requirements import Basis, BudgetSpec, CostKind
from tripplan.render.itinerary_html import render_itinerary_html

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


def test_produces_a_complete_html_document(mk):
    out = render_itinerary_html(_itin(mk), mk.facts(), mk.reqs())
    assert out.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in out


def test_styles_are_inlined_no_external_requests(mk):
    """断网也要能看——任何外链都是 bug。"""
    out = render_itinerary_html(_itin(mk), mk.facts(), mk.reqs())
    assert "<style>" in out
    assert not re.search(r'(src|href)\s*=\s*["\']https?://', out)
    assert "url(" not in out


def test_renders_every_day_and_activity(mk):
    out = render_itinerary_html(_itin(mk), mk.facts(), mk.reqs())
    assert "2026-10-01" in out
    assert "清水寺" in out and "某食堂" in out


def test_transit_legs_appear_between_activities(mk):
    facts = mk.facts(routes=[mk.route("d1", "d1a1", "d1a2", 40)])
    out = render_itinerary_html(_itin(mk), facts, mk.reqs())
    assert "40" in out
    assert out.index("清水寺") < out.index("40") < out.index("某食堂")


def test_unverified_transit_is_shown_not_blank(mk):
    facts = mk.facts(gaps=[mk.gap(GapKind.ROUTE_UNAVAILABLE, "d1a1->d1a2", "高德限流")])
    out = render_itinerary_html(_itin(mk), facts, mk.reqs())
    assert "未能核实" in out


def test_day_map_is_embedded_as_data_uri(mk):
    png = b"\x89PNG-fake"
    out = render_itinerary_html(_itin(mk), mk.facts(), mk.reqs(), day_maps={"d1": png})
    assert base64.b64encode(png).decode() in out
    assert "data:image/png;base64," in out


def test_missing_day_map_degrades_gracefully(mk):
    out = render_itinerary_html(_itin(mk), mk.facts(), mk.reqs(), day_maps={})
    assert "<img" not in out
    assert "清水寺" in out


def test_issues_are_colour_coded_by_severity(mk):
    itin = _itin(mk)
    itin.issues = [
        Issue(Severity.BLOCKING, Source.RULE, "R2", "通勤不够"),
        Issue(Severity.WARNING, Source.RULE, "R8", "没安排晚餐"),
    ]
    out = render_itinerary_html(itin, mk.facts(), mk.reqs())
    assert "sev-blocking" in out and "sev-warning" in out


def test_estimated_amounts_are_marked_distinct_from_verified(mk):
    reqs = mk.reqs(
        budget=Field(
            BudgetSpec(
                Decimal("15000"), "JPY", Basis.TOTAL, frozenset({CostKind.MEAL})
            ),
            Origin.USER,
        )
    )
    out = render_itinerary_html(_itin(mk), mk.facts(), reqs)
    assert "cost-estimated" in out
    assert "已核实" in out and "未知" in out


def test_html_escapes_user_and_model_content(mk):
    itin = _itin(mk)
    itin.days[0].activities[0].poi_query = '<script>alert("x")</script>'
    out = render_itinerary_html(itin, mk.facts(), mk.reqs())
    assert "<script>alert" not in out
    assert "&lt;script&gt;" in out


def test_output_is_stable_across_repeated_renders(mk):
    args = (_itin(mk), mk.facts(), mk.reqs())
    assert render_itinerary_html(*args) == render_itinerary_html(*args)


# ---------- Amendments over the brief ----------


def test_verified_cost_renders_cost_verified_class_on_activity_line(mk):
    """必须断言 CSS class，而不是「已核实」这个字符串——

    该字符串在花费汇总表（ledger）里也会出现，就算活动行自己的
    verified/estimated 判断整个坏掉（比如把枚举比较错写成按值比较），
    只要行程里还有别的已核实花费，汇总表照样会显示「已核实」，
    单纯断言字符串不会因为这个 bug 变红。
    所以这里把搜索窗口限定在 POI 名字紧随其后的一小段文本里，
    只覆盖活动行自己吐出的那个 <span>，不会碰到后面汇总表里的那一份。
    """
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
                        query="清水寺",
                        cost=Money(Decimal("500"), "JPY", Confidence.VERIFIED, "ctrip"),
                    ),
                ],
            )
        ]
    )
    out = render_itinerary_html(itin, mk.facts(), mk.reqs())
    # 只取这一天的 <section> 内容（活动行自己吐出的部分），
    # 不要越界碰到 </section> 之后的花费汇总表——那张表里同一个
    # class="cost-verified" 是写死在「已核实」那一行上的字面量，
    # 与这次活动的核实状态判断是否正确无关，会把测试变成假阳性。
    day_html = out[out.index('<div class="act">') : out.index("</section>")]
    assert "cost-verified" in day_html


def test_ledger_shows_currency_mismatch_caveat(mk):
    """混币种（比如住宿计 CNY、餐食计 JPY）不能悄悄漏掉部分花费不计入合计——

    这是唯一会被转发给同行人看的文档，没有这条提示，读者会看到一个
    看起来很确定的合计数，却不知道它已经漏算了别的币种。
    """
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
                        "10:00",
                        query="酒店",
                        cost=Money(
                            Decimal("10000"), "CNY", Confidence.VERIFIED, "ctrip"
                        ),
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
    out = render_itinerary_html(itin, mk.facts(), mk.reqs())
    assert "不一致" in out
    assert "未计入合计" in out
