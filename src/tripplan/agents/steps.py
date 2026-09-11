"""把 LLM 的结构化输出转成领域对象。每个步骤一个函数。"""

import json
from dataclasses import dataclass, replace
from datetime import date as Date
from datetime import datetime, time
from decimal import Decimal
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


class ParseError(Exception):
    """字段/活动解析失败的统一出口。

    上一轮把 Origin(...) 挪回 try、给 party 补了类型检查之后，复审又在
    budget.amount 上踩到同一类坑：Decimal(str("五千")) 抛的是
    decimal.InvalidOperation——它是 ArithmeticError，不是 ValueError 也不是
    TypeError，不在调用点的 except 元组里，一样会把整条抽取炸穿。
    与其继续在每个调用点枚举"这次又冒出了哪种异常"，不如让解析函数自己把
    内部的异常收敛成这一种，调用点只认 ParseError——新解析器以后想抛什么
    原始异常都不会漏网。
    """


def _safe(fn):
    """把一个"可能失败"的解析函数包成"失败只抛 ParseError"的版本。

    调用点因此只需要写 `except ParseError`，不必再为每一种可能的原始异常
    （KeyError/ValueError/TypeError/InvalidOperation/……）单独记一笔。
    """

    def wrapped(v):
        try:
            return fn(v)
        except ParseError:
            raise
        except Exception as e:
            raise ParseError(str(e)) from e

    return wrapped


_parse_origin = _safe(lambda raw: Origin(raw) if raw else Origin.MODEL)


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
    "destination": _safe(lambda v: str(v)),
    "dates": _safe(
        lambda v: DateRange(
            Date.fromisoformat(v["start"]), Date.fromisoformat(v["end"])
        )
    ),
    "party": _safe(_parse_party),
    "arrival": _safe(
        lambda v: Transfer(datetime.fromisoformat(v["at"]), v.get("mode", ""))
    ),
    "departure": _safe(
        lambda v: Transfer(datetime.fromisoformat(v["at"]), v.get("mode", ""))
    ),
    "budget": _safe(
        lambda v: BudgetSpec(
            Decimal(str(v["amount"])),
            v.get("currency", "CNY"),
            Basis(v.get("basis", "TOTAL")),
            frozenset(CostKind(k) for k in v.get("includes", [])),
        )
    ),
    "styles": _safe(_parse_str_list),
    "pace": _safe(Pace),
    "must_visit": _safe(_parse_str_list),
    "avoid": _safe(_parse_str_list),
    "lodging_area": _safe(str),
    "constraints": _safe(_parse_str_list),
}


def _to_field(name: str, raw: dict | None) -> Field:
    if not raw or raw.get("value") is None:
        return Field()
    try:
        value = _PARSERS[name](raw["value"])
        origin = _parse_origin(raw.get("origin"))
    except ParseError:
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


def _parse_angle(raw: dict) -> Angle:
    return Angle(raw["key"], raw["title"], raw.get("description", ""))


_parse_angle_safe = _safe(_parse_angle)


def pick_angles(reqs: Requirements, deps, ctx, n: int = 3) -> list[Angle]:
    """run_agent 只校验顶层 required（["angles"]），不会递归进每个角度的
    ["key","title"]——缺字段的角度条目要跳过而不是拖垮整批候选。但角度这
    一步没有"部分成功也能用"的余地：跳到一个都不剩时，不能悄悄返回空
    列表——下游会拿着零候选继续跑，既不会规划出任何东西，也没留下任何
    能诊断的线索，所以必须在这里就报错。"""
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
    angles = []
    for raw in data["angles"]:
        try:
            angles.append(_parse_angle_safe(raw))
        except ParseError:
            continue  # 单个角度解析不出来就跳过，不拖累其余候选
        if len(angles) == n:
            break
    if not angles:
        raise ValueError("模型返回的角度候选没有一个能解析——没有可用角度")
    keys = [a.key for a in angles]
    if len(set(keys)) != len(keys):
        # key 重复没有安全的默认值可退——不像坏值那样能当没给，这里没有
        # "半个角度"的概念，交给编排层去处理（例如重跑一次）。
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


def _parse_critic_issue(raw: dict) -> Issue:
    try:
        severity = Severity(raw["severity"])
    except ValueError:
        severity = Severity.SUGGESTION  # 拿不准就往轻里判——枚举值不认识
        # 不算这条点评彻底解析失败，只是判得轻一点
    where = DayRef(raw["where_day"]) if raw.get("where_day") else None
    return Issue(
        severity=severity,
        source=Source.CRITIC,
        code="CRITIC",
        message=raw["message"],
        where=where,
    )


_parse_critic_issue_safe = _safe(_parse_critic_issue)


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
            out.append(_parse_critic_issue_safe(raw))
        except ParseError:
            continue  # 一条点评解析不出来，丢的是一个意见，不是整份点评
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
        except ParseError:
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


_parse_activity = _safe(_to_activity)


def _parse_day_shell(raw_day: dict) -> tuple[Date, list]:
    """解析一天的骨架：date 本身，以及 activities 是不是一个数组。

    这两者任一解析不出来，整天都没法安放——没有日期挂不到时间线上，
    activities 不是数组也没什么可迭代的——所以这是与"活动级"故障不同的
    粒度：活动级故障只丢一个活动，日级故障要丢一整天，不能试图从一个
    连骨架都立不住的结构里硬凑活动出来。"""
    date = Date.fromisoformat(raw_day["date"])
    activities = raw_day["activities"]
    if not isinstance(activities, list):
        raise TypeError("activities 必须是数组")
    return date, activities


_parse_day_shell_safe = _safe(_parse_day_shell)


def _to_itinerary(data: dict, angle: Angle) -> Itinerary:
    """run_agent 只校验顶层 required（["days"]），不会递归进每一天 /
    每个活动的字段——所以一天的 date 格式不对、缺 activities 键，或者
    一个活动缺 poi_query / start 格式不对 / cost.amount 不是数字，都可能
    在这里炸出来。丢掉整份行程会连带炸掉模型排对的其他天/其他活动，静默
    跳过又违反了这个项目一路坚持的规矩：解析不了的东西要留痕，不能装作
    没发生。所以按两级粒度分别兜底：
    - 日级：这一天的骨架（date / activities 本身）解析不出来，跳过整天，
      记一条 WARNING（这一天从没存在过，没有 day_id 可指）。
    - 活动级：日骨架没问题，但其中某个活动解析不出来，只跳过那一个活动，
      记一条 WARNING 并指向这一天最终会被分配到的 day_id。

    两处都用 _safe 收敛内部的异常——不必在 except 里手动枚举
    KeyError/ValueError/TypeError/InvalidOperation，以后解析逻辑变化想抛
    什么都不会漏网。"""
    days = []
    issues: list[Issue] = []
    for raw_index, raw_day in enumerate(data["days"], start=1):
        try:
            date, raw_activities = _parse_day_shell_safe(raw_day)
        except ParseError as e:
            issues.append(
                Issue(
                    severity=Severity.WARNING,
                    source=Source.RULE,
                    code="UNPARSEABLE_DAY",
                    message=f"原始第 {raw_index} 天解析失败，已跳过：{e}",
                    where=None,  # 这一天没能生成，没有 day_id 可指
                )
            )
            continue

        # 只在这一天真正被保留时才计数，与 assign_ids 后续按 itin.days
        # 最终顺序分配的编号对齐——中途丢掉的天不占编号。
        day_number = len(days) + 1
        day_id = f"d{day_number}"
        acts = []
        for act_index, raw in enumerate(raw_activities, start=1):
            try:
                acts.append(_parse_activity(raw))
            except ParseError as e:
                issues.append(
                    Issue(
                        severity=Severity.WARNING,
                        source=Source.RULE,
                        code="UNPARSEABLE_ACTIVITY",
                        message=f"第 {day_number} 天第 {act_index} 个活动解析失败，"
                        f"已跳过：{e}",
                        where=DayRef(day_id),
                    )
                )
        days.append(
            Day(id="", date=date, activities=acts, lodging=raw_day.get("lodging"))
        )
    return Itinerary(angle=angle, days=days, issues=issues)
