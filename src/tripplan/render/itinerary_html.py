"""单文件自包含 HTML。CSS 内联、图片 base64 —— 断网也能看。

触网的 fetch_day_maps 与纯函数的 render_itinerary_html 分开：
渲染测试不需要 provider，同一份 state.json 反复渲染结果一致。
"""

import base64
from html import escape

from tripplan.models.common import Confidence
from tripplan.models.facts import GapKind, Resolved
from tripplan.models.issue import Severity
from tripplan.providers.base import ProviderError
from tripplan.validation.budget import build_ledger

_SEV_CLASS = {
    Severity.BLOCKING: "sev-blocking",
    Severity.WARNING: "sev-warning",
    Severity.SUGGESTION: "sev-suggestion",
}

_CSS = """
:root { color-scheme: light dark; }
body { font: 16px/1.6 -apple-system, "PingFang SC", sans-serif;
       max-width: 46rem; margin: 2rem auto; padding: 0 1rem; }
h1 { margin-bottom: .2rem; }
.angle { color: #666; margin-top: 0; }
.day { border: 1px solid #ddd; border-radius: 10px; padding: 1rem;
       margin: 1.2rem 0; }
.day h2 { margin: 0 0 .4rem; font-size: 1.15rem; }
.lodging { color: #666; font-size: .9rem; margin: 0 0 .8rem; }
.daymap { width: 100%; border-radius: 8px; margin-bottom: .8rem; }
.act { display: flex; gap: .8rem; padding: .45rem 0; }
.time { flex: 0 0 6.5rem; color: #555; font-variant-numeric: tabular-nums; }
.note { color: #777; font-size: .9rem; }
.transit { margin: .1rem 0 .1rem 7.3rem; color: #888; font-size: .88rem; }
.transit.unverified { font-style: italic; }
.cost-verified { color: #1a7f37; }
.cost-estimated { color: #9a6700; }
.sev-blocking { color: #b3261e; }
.sev-warning  { color: #9a6700; }
.sev-suggestion { color: #666; }
.ledger td { padding: .15rem .8rem .15rem 0; }
.mismatch { color: #9a6700; font-size: .9rem; }
footer { color: #888; font-size: .85rem; margin-top: 2rem; }
@media (prefers-color-scheme: dark) {
  body { background: #16181c; color: #e6e6e6; }
  .day { border-color: #333; }
}
"""


def fetch_day_maps(itin, facts, provider) -> dict[str, bytes]:
    """每天一张带标记与路线的静态图。触网，失败就跳过这一天。"""
    out: dict[str, bytes] = {}
    for day in itin.days:
        points = []
        for act in day.activities:
            res = facts.poi_by_activity.get(act.id)
            if isinstance(res, Resolved):
                points.append(res.fact.coords)
        if not points:
            continue
        polyline = next(
            (r.polyline for r in facts.routes if r.day_id == day.id and r.polyline),
            None,
        )
        try:
            out[day.id] = provider.static_map(points, polyline)
        except ProviderError:
            continue  # 少一张图不值得让整份 HTML 出不来
    return out


def _cost_html(cost) -> str:
    if cost is None:
        return ""
    verified = cost.confidence is Confidence.VERIFIED
    cls = "cost-verified" if verified else "cost-estimated"
    label = "已核实" if verified else "估算"
    return (
        f' <span class="{cls}">{escape(str(cost.amount))} '
        f"{escape(cost.currency)}（{label}）</span>"
    )


def _transit_html(facts, day, prev, nxt) -> str:
    route = facts.route(day.id, prev.id, nxt.id)
    if route is not None:
        return (
            f'<div class="transit">↳ {escape(route.mode.value)} 约 '
            f"{route.duration_min} 分钟"
            f"（{route.distance_m / 1000:.1f} km）</div>"
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
    tail = f"：{escape(why)}" if why else ""
    return f'<div class="transit unverified">↳ 通勤耗时未能核实{tail}</div>'


def render_itinerary_html(itin, facts, reqs, day_maps=None) -> str:
    day_maps = day_maps or {}
    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{escape(itin.angle.title)}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        f"<h1>{escape(itin.angle.title)}</h1>",
    ]
    if itin.angle.description:
        parts.append(f'<p class="angle">{escape(itin.angle.description)}</p>')

    for day in itin.days:
        parts.append('<section class="day">')
        parts.append(f"<h2>{day.date.isoformat()}</h2>")
        if day.lodging:
            parts.append(f'<p class="lodging">住宿：' f"{escape(day.lodging)}</p>")
        if day.id in day_maps:
            b64 = base64.b64encode(day_maps[day.id]).decode("ascii")
            parts.append(
                f'<img class="daymap" alt="当日路线" '
                f'src="data:image/png;base64,{b64}">'
            )
        for idx, act in enumerate(day.activities):
            parts.append(
                '<div class="act">'
                f'<span class="time">{act.start:%H:%M}–{act.end:%H:%M}</span>'
                f"<span><strong>{escape(act.poi_query)}</strong>"
                f"{_cost_html(act.cost)}"
                + (f'<div class="note">{escape(act.note)}</div>' if act.note else "")
                + "</span></div>"
            )
            if idx + 1 < len(day.activities):
                parts.append(_transit_html(facts, day, act, day.activities[idx + 1]))
        parts.append("</section>")

    parts += _ledger_html(itin, reqs)
    parts += _issues_html(itin)
    parts += _gaps_html(facts)
    parts += [
        "<footer>由 tripplan 生成。标「估算」「未核实」的信息请自行确认。" "</footer>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts)


def _ledger_html(itin, reqs) -> list[str]:
    led = build_ledger(itin, reqs)
    if led.verified_count == led.estimated_count == led.unknown_count == 0:
        return []
    rows = [
        f'<tr><td>已核实</td><td class="cost-verified">{led.verified} '
        f"{escape(led.currency)}</td><td>{led.verified_count} 项</td></tr>",
        f'<tr><td>估算</td><td class="cost-estimated">{led.estimated} '
        f"{escape(led.currency)}</td><td>{led.estimated_count} 项</td></tr>",
        f"<tr><td>未知</td><td>—</td><td>{led.unknown_count} 项</td></tr>",
    ]
    if led.budget_limit is not None:
        rows.append(
            f"<tr><td>预算</td><td>{led.budget_limit} "
            f"{escape(led.currency)}</td><td></td></tr>"
        )
    if led.currency_mismatch:
        rows.append(
            '<tr><td colspan="3" class="mismatch">⚠️ 存在与预算币种不一致的'
            "花费，未计入合计</td></tr>"
        )
    return ["<h2>花费</h2>", '<table class="ledger">', *rows, "</table>"]


def _issues_html(itin) -> list[str]:
    if not itin.issues:
        return []
    items = [
        f'<li class="{_SEV_CLASS[i.severity]}">{escape(i.message)}</li>'
        for i in itin.issues
    ]
    return ["<h2>遗留问题</h2>", "<ul>", *items, "</ul>"]


def _gaps_html(facts) -> list[str]:
    if not facts.gaps:
        return []
    items = [f"<li>{escape(g.subject)}：{escape(g.detail)}</li>" for g in facts.gaps]
    return ["<h2>未核实的信息</h2>", "<ul>", *items, "</ul>"]
