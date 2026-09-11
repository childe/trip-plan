"""把 LLM 的结构化输出转成领域对象。每个步骤一个函数。"""

import json
from dataclasses import dataclass, replace
from datetime import date as Date
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from enum import Enum
from functools import lru_cache
from pathlib import Path

from tripplan.agents.runner import run_agent
from tripplan.agents.schemas import (
    ANGLES_SCHEMA,
    CRITIQUE_SCHEMA,
    FEEDBACK_SCHEMA,
    ITINERARY_SCHEMA,
    REQUIREMENTS_SCHEMA,
)
from tripplan.agents.tools import build_planning_tools
from tripplan.llm.config import Role
from tripplan.models.common import Confidence, Field, Money, Origin
from tripplan.models.issue import DayRef, Issue, Severity, Source
from tripplan.models.itinerary import (
    Activity,
    Angle,
    Category,
    Day,
    Itinerary,
    assign_ids,
)
from tripplan.models.requirements import (
    Basis,
    BudgetSpec,
    CostKind,
    DateRange,
    Pace,
    Party,
    Requirements,
    Transfer,
    describe_value,
)

_PROMPTS = Path(__file__).parent / "prompts"


@lru_cache(maxsize=None)
def _prompt(name: str) -> str:
    return (_PROMPTS / f"{name}.md").read_text(encoding="utf-8")


class Scale(Enum):
    INCREMENTAL = "INCREMENTAL"
    REWRITE = "REWRITE"


@dataclass(frozen=True)
class FeedbackDelta:
    patches_requirements: bool
    patch: dict
    scale: Scale


# ---------- 值解析 ----------


def _parse_party(v) -> Party:
    if not isinstance(v, dict):
        # party 是三项必填之一：解析不了要降级为「没给」，不能让 AttributeError
        # 这种没被上面 except 元组接住的异常把整个 collect/apply_patch 炸穿。
        raise TypeError("party 必须是对象")
    return Party(
        adults=v.get("adults", 1),
        children=v.get("children", 0),
        seniors=v.get("seniors", 0),
    )


def _parse_str_list(v) -> list[str]:
    if not isinstance(v, list):
        # list("环球影城") 会把字符串拆成单字——不炸但更糟：下游拿单字去配
        # POI，全部配不上，反而误判成「must_visit 没排进去」的 BLOCKING。
        # 宁可整项当没给，也不能把约束悄悄拆坏。
        raise TypeError("需要数组，不是字符串")
    return list(v)


_PARSERS = {
    "destination": lambda v: str(v),
    "dates": lambda v: DateRange(
        Date.fromisoformat(v["start"]), Date.fromisoformat(v["end"])
    ),
    "party": _parse_party,
    "arrival": lambda v: Transfer(datetime.fromisoformat(v["at"]), v.get("mode", "")),
    "departure": lambda v: Transfer(datetime.fromisoformat(v["at"]), v.get("mode", "")),
    "budget": lambda v: BudgetSpec(
        Decimal(str(v["amount"])),
        v.get("currency", "CNY"),
        Basis(v.get("basis", "TOTAL")),
        frozenset(CostKind(k) for k in v.get("includes", [])),
    ),
    "styles": _parse_str_list,
    "pace": Pace,
    "must_visit": _parse_str_list,
    "avoid": _parse_str_list,
    "lodging_area": str,
    "constraints": _parse_str_list,
}


def _to_field(name: str, raw: dict | None) -> Field:
    if not raw or raw.get("value") is None:
        return Field()
    try:
        value = _PARSERS[name](raw["value"])
        # origin 的转换必须也在 try 里——"SYSTEM"、"user"（大小写不对）这类
        # 不合法取值会让 Origin(...) 抛 ValueError，之前这行在 try 外面，
        # 一次坏 origin 就能炸穿整个 collect()。
        raw_origin = raw.get("origin")
        origin = Origin(raw_origin) if raw_origin else Origin.MODEL
    except (KeyError, ValueError, TypeError):
        return Field()  # 解析不了就当没给，不要塞个坏值进去
    return Field(
        value=value,
        origin=origin,
        confirmed=False,  # 确认是用户的动作，不是抽取的副产品
        rationale=raw.get("rationale", ""),
    )


# ---------- 步骤 ----------


def collect(raw_request: str, deps, ctx) -> Requirements:
    data = run_agent(
        system_prompt=_prompt("collect"),
        user_prompt=raw_request,
        tools=None,
        output_schema=REQUIREMENTS_SCHEMA,
        role=Role.CLASSIFIER,
        ctx=ctx,
        client=deps.client,
        tool_impls={},
    )
    return Requirements(**{name: _to_field(name, data.get(name)) for name in _PARSERS})


def pick_angles(reqs: Requirements, deps, ctx, n: int = 3) -> list[Angle]:
    data = run_agent(
        system_prompt=_prompt("angle").replace("{n}", str(n)),
        user_prompt=_describe_requirements(reqs),
        tools=None,
        output_schema=ANGLES_SCHEMA,
        role=Role.ANGLE,
        ctx=ctx,
        client=deps.client,
        tool_impls={},
    )
    angles = [
        Angle(a["key"], a["title"], a.get("description", ""))
        for a in data["angles"][:n]
    ]
    keys = [a.key for a in angles]
    if len(set(keys)) != len(keys):
        raise ValueError(f"角度 key 重复：{keys}")
    return angles


def generate(
    reqs: Requirements, angle: Angle, deps, ctx, avoid_poi_ids=()
) -> Itinerary:
    prompt = [
        _describe_requirements(reqs),
        f"\n本方案的切入角度：{angle.title} —— {angle.description}",
    ]
    if avoid_poi_ids:
        prompt.append(
            "\n以下 POI id 已经出现在其他候选方案里，请尽量避开，"
            f"给出实质不同的选择：{sorted(avoid_poi_ids)}"
        )
    return _plan(prompt, reqs, angle, deps, ctx)


def revise(itin: Itinerary, reqs: Requirements, issues, deps, ctx) -> Itinerary:
    prompt = [
        _describe_requirements(reqs),
        f"\n本方案的切入角度：{itin.angle.title}",
        "\n当前行程：\n" + json.dumps(_itinerary_to_json(itin), ensure_ascii=False),
        "\n必须解决的问题：",
        *[f"- [{i.severity.value}] {i.message}" for i in issues],
        "\n请在现有安排基础上修改，保留没有问题的部分。",
    ]
    return _plan(prompt, reqs, itin.angle, deps, ctx)


def _plan(prompt_parts, reqs, angle, deps, ctx) -> Itinerary:
    city = reqs.destination.value or ""
    specs, impls = build_planning_tools(deps.provider, city)
    data = run_agent(
        system_prompt=_prompt("plan"),
        user_prompt="\n".join(prompt_parts),
        tools=specs,
        output_schema=ITINERARY_SCHEMA,
        role=Role.PLANNER,
        ctx=ctx,
        client=deps.client,
        tool_impls=impls,
    )
    return assign_ids(_to_itinerary(data, angle))


def run_llm_critic(itin, reqs: Requirements, deps, ctx) -> list[Issue]:
    body = (
        "行程：\n" + json.dumps(_itinerary_to_json(itin), ensure_ascii=False)
        if itin is not None
        else "行程：（空）"
    )
    data = run_agent(
        system_prompt=_prompt("critic"),
        user_prompt=f"{_describe_requirements(reqs)}\n\n{body}",
        tools=None,
        output_schema=CRITIQUE_SCHEMA,
        role=Role.CRITIC,
        ctx=ctx,
        client=deps.client,
        tool_impls={},
    )
    out = []
    for raw in data["issues"]:
        try:
            severity = Severity(raw["severity"])
        except ValueError:
            severity = Severity.SUGGESTION  # 拿不准就往轻里判
        where = DayRef(raw["where_day"]) if raw.get("where_day") else None
        out.append(
            Issue(
                severity=severity,
                source=Source.CRITIC,
                code="CRITIC",
                message=raw["message"],
                where=where,
            )
        )
    return out


def classify_feedback(text: str, reqs: Requirements, deps, ctx) -> FeedbackDelta:
    data = run_agent(
        system_prompt=_prompt("classify"),
        user_prompt=f"{_describe_requirements(reqs)}\n\n用户反馈：{text}",
        tools=None,
        output_schema=FEEDBACK_SCHEMA,
        role=Role.CLASSIFIER,
        ctx=ctx,
        client=deps.client,
        tool_impls={},
    )
    try:
        scale = Scale(data.get("scale", "INCREMENTAL"))
    except ValueError:
        scale = Scale.INCREMENTAL
    return FeedbackDelta(
        bool(data["patches_requirements"]), data.get("patch") or {}, scale
    )


def apply_patch(reqs: Requirements, patch: dict) -> Requirements:
    """把 patch 应用到需求上。用户改的字段一律标 USER + 已确认。"""
    updates = {}
    for name, raw_value in patch.items():
        if name not in _PARSERS:
            continue  # 未知字段忽略，不炸
        try:
            value = _PARSERS[name](raw_value)
        except (KeyError, ValueError, TypeError):
            continue
        updates[name] = Field(value=value, origin=Origin.USER, confirmed=True)
    return replace(reqs, **updates) if updates else reqs


# ---------- 渲染给模型看的文本 ----------


def _describe_requirements(reqs: Requirements) -> str:
    lines = []
    for name in _PARSERS:
        f = getattr(reqs, name)
        if f.value is None:
            continue
        mark = "" if f.origin is Origin.USER else "（推断）"
        lines.append(f"- {name}{mark}: {describe_value(f.value)}")
    return "需求：\n" + ("\n".join(lines) if lines else "（尚未收集）")


def _itinerary_to_json(itin: Itinerary) -> dict:
    return {
        "days": [
            {
                "date": d.date.isoformat(),
                "lodging": d.lodging,
                "activities": [
                    {
                        "poi_query": a.poi_query,
                        "start": a.start.isoformat(timespec="minutes"),
                        "end": a.end.isoformat(timespec="minutes"),
                        "category": a.category.value,
                        "cost": (
                            None
                            if a.cost is None
                            else {
                                "amount": str(a.cost.amount),
                                "currency": a.cost.currency,
                            }
                        ),
                        "indoor": a.indoor,
                        "note": a.note,
                    }
                    for a in d.activities
                ],
            }
            for d in itin.days
        ]
    }


def _to_activity(raw: dict) -> Activity:
    cost = None
    if raw.get("cost"):
        cost = Money(
            Decimal(str(raw["cost"]["amount"])),
            raw["cost"].get("currency", "CNY"),
            Confidence.ESTIMATED,  # 模型给的一律是估算
            "llm:知识",
        )
    try:
        category = Category(raw["category"])
    except ValueError:
        category = Category.SIGHT
    return Activity(
        id="",
        day_id="",
        poi_query=raw["poi_query"],
        start=time.fromisoformat(raw["start"]),
        end=time.fromisoformat(raw["end"]),
        category=category,
        cost=cost,
        indoor=bool(raw.get("indoor", False)),
        note=raw.get("note", ""),
    )


def _to_itinerary(data: dict, angle: Angle) -> Itinerary:
    """run_agent 只校验顶层 required（["days"]），不会递归进每个活动的字段——
    所以一个活动缺 poi_query / start 格式不对 / cost.amount 缺失都可能在这里
    炸出来。丢掉整份行程会连带炸掉模型排对的其他活动，静默跳过又违反了这个
    项目一路坚持的规矩：解析不了的东西要留痕，不能装作没发生。所以每个活动
    单独兜底：解析不出来就跳过它，同时在 itinerary.issues 里记一条 WARNING，
    留给下游（渲染 / 人工复核）看到「这里少了什么、为什么」。"""
    days = []
    issues: list[Issue] = []
    for day_index, raw_day in enumerate(data["days"], start=1):
        acts = []
        day_id = f"d{day_index}"  # 与 assign_ids 后续分配的编号对齐
        for act_index, raw in enumerate(raw_day["activities"], start=1):
            try:
                acts.append(_to_activity(raw))
            except (KeyError, ValueError, TypeError, InvalidOperation) as e:
                issues.append(
                    Issue(
                        severity=Severity.WARNING,
                        source=Source.RULE,
                        code="UNPARSEABLE_ACTIVITY",
                        message=f"第 {day_index} 天第 {act_index} 个活动解析失败，"
                        f"已跳过：{e}",
                        where=DayRef(day_id),
                    )
                )
        days.append(
            Day(
                id="",
                date=Date.fromisoformat(raw_day["date"]),
                activities=acts,
                lodging=raw_day.get("lodging"),
            )
        )
    return Itinerary(angle=angle, days=days, issues=issues)
