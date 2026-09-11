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
