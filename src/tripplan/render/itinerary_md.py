"""Markdown 行程单。只读 FactSnapshot，不触网。"""

from tripplan.models.facts import GapKind
from tripplan.models.issue import Severity
from tripplan.validation.budget import build_ledger

_MARK = {Severity.BLOCKING: "🔴", Severity.WARNING: "🟡", Severity.SUGGESTION: "⚪"}


def _transit_line(facts, day, prev, nxt) -> str:
    route = facts.route(day.id, prev.id, nxt.id)
    if route is not None:
        return (
            f"  ↳ {route.mode.value} 约 {route.duration_min} 分钟"
            f"（{route.distance_m / 1000:.1f} km）"
        )
    subject = f"{prev.id}->{nxt.id}"
    why = next(
        (
            g.detail
            for g in facts.gaps
            if g.kind is GapKind.ROUTE_UNAVAILABLE and g.subject == subject
        ),
        "",
    )
    tail = f"：{why}" if why else ""
    return f"  ↳ _通勤耗时未能核实{tail}_"


def render_itinerary_md(itin, facts, reqs) -> str:
    lines = [f"# {itin.angle.title}", ""]
    if itin.angle.description:
        lines += [f"_{itin.angle.description}_", ""]

    for day in itin.days:
        lines.append(f"## {day.date.isoformat()}")
        if day.lodging:
            lines.append(f"住宿：{day.lodging}")
        lines.append("")
        for idx, act in enumerate(day.activities):
            cost = ""
            if act.cost is not None:
                tag = "已核实" if act.cost.confidence.value == "VERIFIED" else "估算"
                cost = f" — {act.cost.amount} {act.cost.currency}（{tag}）"
            lines.append(
                f"- **{act.start:%H:%M}–{act.end:%H:%M}** {act.poi_query}" f"{cost}"
            )
            if act.note:
                lines.append(f"  {act.note}")
            if idx + 1 < len(day.activities):
                lines.append(_transit_line(facts, day, act, day.activities[idx + 1]))
        lines.append("")

    lines += _ledger_section(itin, reqs)
    lines += _issues_section(itin)
    lines += _gaps_section(facts)
    return "\n".join(lines)


def _ledger_section(itin, reqs) -> list[str]:
    led = build_ledger(itin, reqs)
    if led.verified_count == led.estimated_count == led.unknown_count == 0:
        return []
    out = [
        "## 花费",
        "",
        f"- 已核实：{led.verified} {led.currency}" f"（{led.verified_count} 项）",
        f"- 估算：{led.estimated} {led.currency}"
        f"（{led.estimated_count} 项，来源：模型知识）",
        f"- 未知：{led.unknown_count} 项",
    ]
    if led.budget_limit is not None:
        out.append(f"- 预算：{led.budget_limit} {led.currency}")
    if led.currency_mismatch:
        out.append("- ⚠️ 存在与预算币种不一致的花费，未计入合计")
    return out + [""]


def _issues_section(itin) -> list[str]:
    if not itin.issues:
        return []
    return (
        ["## 遗留问题", ""]
        + [f"- {_MARK[i.severity]} {i.message}" for i in itin.issues]
        + [""]
    )


def _gaps_section(facts) -> list[str]:
    if not facts.gaps:
        return []
    return (
        ["## 未核实的信息", "", "以下内容没有可靠数据源，请自行确认：", ""]
        + [f"- {g.subject}：{g.detail}" for g in facts.gaps]
        + [""]
    )
