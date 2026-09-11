"""每天一张静态地图。触网，失败就跳过这一天——不值得让整份 HTML 出不来。

与 `render/itinerary_html.py` 分开：`render/` 下的模块禁止任何网络调用，
而这里就是那一小块需要触网的部分。渲染只吃这里已经拿到的字节。
"""

from tripplan.models.facts import Resolved
from tripplan.providers.base import ProviderError


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
