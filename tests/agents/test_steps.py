import json
from datetime import date
from decimal import Decimal

import pytest

from tripplan.agents.limits import SlotContext, SlotLimits
from tripplan.agents.steps import (
    Scale,
    apply_patch,
    classify_feedback,
    collect,
    generate,
    pick_angles,
    revise,
    run_llm_critic,
)
from tripplan.deps import Deps
from tripplan.llm.client import FakeLlm, LlmResponse, Usage
from tripplan.models.common import Field, Origin
from tripplan.models.issue import Severity, Source
from tripplan.models.itinerary import Angle, Category
from tripplan.models.requirements import Pace, Party, Requirements
from tripplan.providers.fake import FakeProvider


def _resp(payload) -> LlmResponse:
    return LlmResponse(
        "end_turn", json.dumps(payload, ensure_ascii=False), [], Usage(100, 50)
    )


def _deps(*responses) -> Deps:
    return Deps(
        client=FakeLlm(list(responses)),
        provider=FakeProvider(pois={"清水寺": [("B001", 34.9949, 135.785)]}),
    )


def _ctx():
    return SlotContext(SlotLimits())


# ---------- collect ----------

COLLECTED = {
    "destination": {"value": "京都", "origin": "USER", "rationale": ""},
    "dates": {
        "value": {"start": "2026-10-01", "end": "2026-10-05"},
        "origin": "USER",
        "rationale": "",
    },
    "party": {
        "value": {"adults": 2, "children": 0, "seniors": 0},
        "origin": "USER",
        "rationale": "",
    },
    "pace": {"value": "RELAXED", "origin": "MODEL", "rationale": "带老人"},
    "budget": {"value": None, "origin": None, "rationale": ""},
    "styles": {"value": ["美食"], "origin": "USER", "rationale": ""},
}


def test_collect_builds_requirements_with_origins():
    reqs = collect("十一去京都5天两人", _deps(_resp(COLLECTED)), _ctx())
    assert reqs.destination.value == "京都"
    assert reqs.destination.origin is Origin.USER
    assert reqs.pace.value is Pace.RELAXED
    assert reqs.pace.origin is Origin.MODEL
    assert reqs.pace.rationale == "带老人"


def test_collect_leaves_unknown_fields_empty():
    reqs = collect("x", _deps(_resp(COLLECTED)), _ctx())
    assert reqs.budget.value is None
    assert reqs.budget.origin is None


def test_collect_never_marks_anything_confirmed():
    """确认是用户的动作，不是抽取的副产品。"""
    reqs = collect("x", _deps(_resp(COLLECTED)), _ctx())
    assert all(
        not getattr(reqs, n).confirmed
        for n in ("destination", "dates", "party", "pace")
    )


def test_collect_parses_dates_and_party():
    reqs = collect("x", _deps(_resp(COLLECTED)), _ctx())
    assert reqs.dates.value.start == date(2026, 10, 1)
    assert reqs.dates.value.days == 5
    assert reqs.party.value == Party(adults=2)


def test_collect_ignores_invalid_origin_value():
    """origin 不是合法枚举值时，整条字段当没给，而不是让 collect 崩掉。"""
    payload = dict(COLLECTED)
    payload["destination"] = {"value": "京都", "origin": "SYSTEM", "rationale": ""}
    reqs = collect("x", _deps(_resp(payload)), _ctx())
    assert reqs.destination.value is None
    assert reqs.destination.origin is None


def test_collect_ignores_lowercase_origin_value():
    payload = dict(COLLECTED)
    payload["destination"] = {"value": "京都", "origin": "user", "rationale": ""}
    reqs = collect("x", _deps(_resp(payload)), _ctx())
    assert reqs.destination.value is None
    assert reqs.destination.origin is None


def test_collect_falls_back_to_empty_when_party_is_not_an_object():
    payload = dict(COLLECTED)
    payload["party"] = {"value": "两人", "origin": "USER", "rationale": ""}
    reqs = collect("x", _deps(_resp(payload)), _ctx())
    assert reqs.party.value is None
    assert reqs.party.origin is None


def test_collect_does_not_character_split_a_bare_string_must_visit():
    """must_visit 是字符串而不是列表时，不能被 list() 拆成单字——
    拆了会让规则 4 拿单字去搜 POI，全部搜不到，误判成 BLOCKING。"""
    payload = dict(COLLECTED)
    payload["must_visit"] = {"value": "环球影城", "origin": "USER", "rationale": ""}
    reqs = collect("x", _deps(_resp(payload)), _ctx())
    assert reqs.must_visit.value is None


def test_collect_falls_back_when_budget_amount_is_not_numeric():
    """Decimal(str(v["amount"])) 对中文数字这类非数字字符串会抛
    decimal.InvalidOperation——它是 ArithmeticError，不是 ValueError，
    不在旧的 except (KeyError, ValueError, TypeError) 元组里，会直接
    炸穿 collect。budget 必须像其他字段一样安全降级。"""
    payload = dict(COLLECTED)
    payload["budget"] = {
        "value": {
            "amount": "五千",
            "currency": "CNY",
            "basis": "TOTAL",
            "includes": [],
        },
        "origin": "USER",
        "rationale": "",
    }
    reqs = collect("x", _deps(_resp(payload)), _ctx())
    assert reqs.budget.value is None
    assert reqs.budget.origin is None


def test_collect_degrades_when_a_parser_raises_an_unexpected_exception_type():
    """回归防线：解析器内部不管抛什么异常类型，收敛之后只应该在调用点
    看到 ParseError 一种——不该出现"这个具体异常类型忘了列进 except 元组"
    这种漏洞（decimal.InvalidOperation 就是这么漏网的）。"""
    from tripplan.agents import steps

    def _boom(_v):
        raise ZeroDivisionError("没被专门枚举过的异常类型")

    original = steps._PARSERS["destination"]
    steps._PARSERS["destination"] = steps._safe(_boom)
    try:
        reqs = collect("x", _deps(_resp(COLLECTED)), _ctx())
    finally:
        steps._PARSERS["destination"] = original
    assert reqs.destination.value is None
    assert reqs.destination.origin is None


# ---------- pick_angles ----------


def test_pick_angles_returns_requested_count():
    payload = {
        "angles": [
            {"key": "A", "title": "古寺巡礼", "description": "世界遗产主线"},
            {"key": "B", "title": "市井美食", "description": "锦市场为轴"},
            {"key": "C", "title": "近郊自然", "description": "岚山与贵船"},
        ]
    }
    angles = pick_angles(Requirements(), _deps(_resp(payload)), _ctx(), n=3)
    assert [a.key for a in angles] == ["A", "B", "C"]
    assert angles[0].title == "古寺巡礼"


def test_pick_angles_rejects_duplicate_keys():
    payload = {
        "angles": [
            {"key": "A", "title": "x", "description": ""},
            {"key": "A", "title": "y", "description": ""},
        ]
    }
    with pytest.raises(ValueError, match="重复"):
        pick_angles(Requirements(), _deps(_resp(payload)), _ctx(), n=2)


def test_pick_angles_skips_entry_missing_title_and_keeps_the_valid_one():
    """run_agent 只校验顶层 required（["angles"]），不会递归进每个角度的
    ["key","title"]——缺 title 的条目应该被跳过，而不是让 KeyError 拖垮
    整批候选角度。"""
    payload = {
        "angles": [
            {"key": "A"},  # 缺 title
            {"key": "B", "title": "市井美食", "description": "锦市场为轴"},
        ]
    }
    angles = pick_angles(Requirements(), _deps(_resp(payload)), _ctx(), n=2)
    assert [a.key for a in angles] == ["B"]


def test_pick_angles_raises_when_no_angle_survives_parsing():
    """跳过坏角度是安全的，但一个都凑不齐时不能悄悄返回空列表——
    这一步本来就不能在零候选的情况下继续，必须给出清楚的报错而不是留下
    一个"看起来成功但什么都没有"的结果。"""
    payload = {"angles": [{"key": "A"}, {"key": "B"}]}  # 都缺 title
    with pytest.raises(ValueError, match="没有"):
        pick_angles(Requirements(), _deps(_resp(payload)), _ctx(), n=3)


# ---------- generate / revise ----------

PLAN = {
    "days": [
        {
            "date": "2026-10-01",
            "lodging": "京都站",
            "activities": [
                {
                    "poi_query": "清水寺",
                    "start": "09:00",
                    "end": "11:00",
                    "category": "SIGHT",
                    "cost": None,
                    "indoor": False,
                    "note": "清晨人少",
                },
                {
                    "poi_query": "某食堂",
                    "start": "12:00",
                    "end": "13:00",
                    "category": "MEAL",
                    "cost": {"amount": "1500", "currency": "JPY"},
                    "indoor": True,
                    "note": "",
                },
            ],
        }
    ]
}


def _angle():
    return Angle("A", "古寺巡礼", "")


def test_generate_builds_itinerary_with_ids_assigned():
    itin = generate(
        Requirements(destination=Field("京都", Origin.USER)),
        _angle(),
        _deps(_resp(PLAN)),
        _ctx(),
    )
    assert itin.days[0].id == "d1"
    assert [a.id for a in itin.days[0].activities] == ["d1a1", "d1a2"]
    assert itin.angle.key == "A"


def test_generate_parses_costs_as_decimal_and_estimated():
    itin = generate(
        Requirements(destination=Field("京都", Origin.USER)),
        _angle(),
        _deps(_resp(PLAN)),
        _ctx(),
    )
    cost = itin.days[0].activities[1].cost
    assert cost.amount == Decimal("1500")
    assert cost.currency == "JPY"
    assert cost.confidence.value == "ESTIMATED"  # 模型给的一律是估算


def test_generate_treats_null_cost_as_unknown():
    itin = generate(
        Requirements(destination=Field("京都", Origin.USER)),
        _angle(),
        _deps(_resp(PLAN)),
        _ctx(),
    )
    assert itin.days[0].activities[0].cost is None


def test_generate_parses_categories():
    itin = generate(
        Requirements(destination=Field("京都", Origin.USER)),
        _angle(),
        _deps(_resp(PLAN)),
        _ctx(),
    )
    assert itin.days[0].activities[1].category is Category.MEAL


def test_generate_passes_avoid_list_into_the_prompt():
    deps = _deps(_resp(PLAN))
    generate(
        Requirements(destination=Field("京都", Origin.USER)),
        _angle(),
        deps,
        _ctx(),
        avoid_poi_ids=frozenset({"B001", "B002"}),
    )
    prompt = deps.client.calls[0].messages[0]["content"]
    assert "B001" in prompt


BROKEN_PLAN = {
    "days": [
        {
            "date": "2026-10-01",
            "lodging": None,
            "activities": [
                {
                    # 缺 poi_query —— 解析必炸
                    "start": "09:00",
                    "end": "11:00",
                    "category": "SIGHT",
                    "cost": None,
                    "indoor": False,
                    "note": "缺 poi_query",
                },
                {
                    "poi_query": "清水寺",
                    "start": "12:00",
                    "end": "13:00",
                    "category": "MEAL",
                    "cost": None,
                    "indoor": True,
                    "note": "",
                },
            ],
        }
    ]
}


def test_generate_skips_malformed_activity_and_keeps_the_good_one():
    """一个活动解析失败不该拖垮整份行程——好的那个必须留下，
    坏的那个要留痕（issues），而不是无声消失或整体报废。"""
    itin = generate(
        Requirements(destination=Field("京都", Origin.USER)),
        _angle(),
        _deps(_resp(BROKEN_PLAN)),
        _ctx(),
    )
    assert len(itin.days[0].activities) == 1
    assert itin.days[0].activities[0].poi_query == "清水寺"
    assert any(i.severity is Severity.WARNING for i in itin.issues)


BROKEN_COST_PLAN = {
    "days": [
        {
            "date": "2026-10-01",
            "lodging": None,
            "activities": [
                {
                    "poi_query": "清水寺",
                    "start": "09:00",
                    "end": "11:00",
                    "category": "SIGHT",
                    "cost": {"amount": "五百", "currency": "JPY"},
                    "indoor": False,
                    "note": "cost.amount 不是数字",
                },
                {
                    "poi_query": "某食堂",
                    "start": "12:00",
                    "end": "13:00",
                    "category": "MEAL",
                    "cost": None,
                    "indoor": True,
                    "note": "",
                },
            ],
        }
    ]
}


def test_generate_skips_activity_with_non_numeric_cost_and_keeps_the_rest():
    """Decimal(str("五百")) 抛的是 decimal.InvalidOperation——它既不是
    ValueError 也不是 TypeError，若调用点只认这两个类型就会漏网，
    炸穿整份行程。"""
    itin = generate(
        Requirements(destination=Field("京都", Origin.USER)),
        _angle(),
        _deps(_resp(BROKEN_COST_PLAN)),
        _ctx(),
    )
    assert len(itin.days[0].activities) == 1
    assert itin.days[0].activities[0].poi_query == "某食堂"
    assert any(i.severity is Severity.WARNING for i in itin.issues)


DAY_LEVEL_BROKEN_DATE_PLAN = {
    "days": [
        {
            "date": "十月一日",  # 非法 ISO 日期——整天没法安放
            "lodging": None,
            "activities": [
                {
                    "poi_query": "清水寺",
                    "start": "09:00",
                    "end": "11:00",
                    "category": "SIGHT",
                    "cost": None,
                    "indoor": False,
                    "note": "",
                },
            ],
        },
        {
            "date": "2026-10-02",
            "lodging": None,
            "activities": [
                {
                    "poi_query": "金阁寺",
                    "start": "09:00",
                    "end": "11:00",
                    "category": "SIGHT",
                    "cost": None,
                    "indoor": False,
                    "note": "",
                },
            ],
        },
    ]
}


def test_generate_skips_day_with_invalid_date_and_keeps_the_good_day():
    """一天的 date 解析不出来，这一天整个没法安放在时间线上——应该跳过
    这一天而不是让 ValueError 拖垮整份行程，包括其它排对了的天。"""
    itin = generate(
        Requirements(destination=Field("京都", Origin.USER)),
        _angle(),
        _deps(_resp(DAY_LEVEL_BROKEN_DATE_PLAN)),
        _ctx(),
    )
    assert len(itin.days) == 1
    assert itin.days[0].date == date(2026, 10, 2)
    assert any(i.severity is Severity.WARNING for i in itin.issues)


DAY_LEVEL_MISSING_ACTIVITIES_PLAN = {
    "days": [
        {"date": "2026-10-01", "lodging": None},  # 完全没有 activities 键
        {
            "date": "2026-10-02",
            "lodging": None,
            "activities": [
                {
                    "poi_query": "金阁寺",
                    "start": "09:00",
                    "end": "11:00",
                    "category": "SIGHT",
                    "cost": None,
                    "indoor": False,
                    "note": "",
                },
            ],
        },
    ]
}


def test_generate_skips_day_missing_activities_key_and_keeps_the_good_day():
    """run_agent 只校验顶层 required（["days"]），不会递归进每一天的
    ["date","activities"]——缺 activities 键的一天要被跳过，而不是让
    KeyError 拖垮整份行程。"""
    itin = generate(
        Requirements(destination=Field("京都", Origin.USER)),
        _angle(),
        _deps(_resp(DAY_LEVEL_MISSING_ACTIVITIES_PLAN)),
        _ctx(),
    )
    assert len(itin.days) == 1
    assert itin.days[0].date == date(2026, 10, 2)
    assert any(i.severity is Severity.WARNING for i in itin.issues)


def test_revise_includes_issues_in_the_prompt():
    from tripplan.models.issue import Issue

    deps = _deps(_resp(PLAN))
    itin = generate(
        Requirements(destination=Field("京都", Origin.USER)),
        _angle(),
        _deps(_resp(PLAN)),
        _ctx(),
    )
    issues = [Issue(Severity.BLOCKING, Source.RULE, "R2", "通勤时间不够")]
    revise(
        itin, Requirements(destination=Field("京都", Origin.USER)), issues, deps, _ctx()
    )
    prompt = deps.client.calls[0].messages[0]["content"]
    assert "通勤时间不够" in prompt


# ---------- critic ----------


def test_critic_returns_issues_sourced_critic():
    payload = {
        "issues": [
            {"severity": "SUGGESTION", "message": "可以加个夜景", "where_day": None},
            {
                "severity": "BLOCKING",
                "message": "第3天完全不符合休闲节奏",
                "where_day": "d3",
            },
        ]
    }
    issues = run_llm_critic(None, Requirements(), _deps(_resp(payload)), _ctx())
    assert [i.severity for i in issues] == [Severity.SUGGESTION, Severity.BLOCKING]
    assert all(i.source is Source.CRITIC for i in issues)
    assert issues[1].where.day_id == "d3"


def test_critic_tolerates_empty_verdict():
    assert (
        run_llm_critic(None, Requirements(), _deps(_resp({"issues": []})), _ctx()) == []
    )


def test_critic_skips_issue_missing_message_and_keeps_the_valid_one():
    """run_agent 只校验顶层 required（["issues"]），不会递归进每条 issue 的
    ["severity","message"]——缺 message 的一条点评是丢了一个意见，不该拖垮
    整份点评。"""
    payload = {
        "issues": [
            {"severity": "SUGGESTION", "where_day": None},  # 缺 message
            {
                "severity": "BLOCKING",
                "message": "第3天完全不符合休闲节奏",
                "where_day": "d3",
            },
        ]
    }
    issues = run_llm_critic(None, Requirements(), _deps(_resp(payload)), _ctx())
    assert len(issues) == 1
    assert issues[0].message == "第3天完全不符合休闲节奏"


# ---------- classify_feedback ----------


def test_classify_detects_itinerary_only_feedback():
    payload = {"patches_requirements": False, "patch": {}, "scale": "INCREMENTAL"}
    delta = classify_feedback(
        "第2天太赶了", Requirements(), _deps(_resp(payload)), _ctx()
    )
    assert delta.patches_requirements is False


def test_classify_detects_requirement_change_with_scale():
    payload = {
        "patches_requirements": True,
        "patch": {"dates": {"start": "2026-10-01", "end": "2026-10-04"}},
        "scale": "INCREMENTAL",
    }
    delta = classify_feedback("改成四天", Requirements(), _deps(_resp(payload)), _ctx())
    assert delta.patches_requirements is True
    assert delta.scale is Scale.INCREMENTAL
    assert "dates" in delta.patch


def test_classify_detects_rewrite_scale():
    payload = {
        "patches_requirements": True,
        "patch": {"destination": "巴黎"},
        "scale": "REWRITE",
    }
    delta = classify_feedback("改去巴黎", Requirements(), _deps(_resp(payload)), _ctx())
    assert delta.scale is Scale.REWRITE


# ---------- apply_patch ----------


def test_apply_patch_sets_value_and_marks_user_origin():
    reqs = Requirements(destination=Field("京都", Origin.MODEL))
    out = apply_patch(reqs, {"destination": "巴黎"})
    assert out.destination.value == "巴黎"
    assert out.destination.origin is Origin.USER  # 用户改的
    assert out.destination.confirmed is True


def test_apply_patch_leaves_other_fields_alone():
    reqs = Requirements(
        destination=Field("京都", Origin.USER), pace=Field(Pace.RELAXED, Origin.MODEL)
    )
    out = apply_patch(reqs, {"destination": "巴黎"})
    assert out.pace.value is Pace.RELAXED
    assert out.pace.origin is Origin.MODEL


def test_apply_patch_parses_structured_values():
    out = apply_patch(
        Requirements(), {"dates": {"start": "2026-10-01", "end": "2026-10-04"}}
    )
    assert out.dates.value.days == 4


def test_apply_patch_ignores_unknown_field():
    out = apply_patch(Requirements(), {"wizardry": 1})
    assert out == Requirements()


def test_apply_patch_falls_back_when_party_is_not_an_object():
    out = apply_patch(Requirements(), {"party": "两人"})
    assert out == Requirements()


def test_apply_patch_falls_back_when_budget_amount_is_not_numeric():
    out = apply_patch(
        Requirements(),
        {
            "budget": {
                "amount": "五千",
                "currency": "CNY",
                "basis": "TOTAL",
                "includes": [],
            }
        },
    )
    assert out == Requirements()
