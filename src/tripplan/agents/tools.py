"""包给 LLM 的工具壳。

与 Provider 分成两层的实际收益：route() 同时被这里和 resolver 调用，
共用 Provider 就共用一份磁盘缓存——LLM 规划时查过的路线，校验时不必再查。
工具负责参数校验与「转成 LLM 友好的结构」，Provider 只管取数。
"""

import inspect
from datetime import datetime

from tripplan.models.common import LatLng, TravelMode
from tripplan.providers.base import GeoProvider, ProviderError


def check_tool_impls(impls: dict) -> None:
    """每个工具实现必须至少有一个必填参数。

    这条契约存在的唯一理由在 llm/backends/openai.py：arguments 解析失败时
    backend 产出 args={}，靠 impl(**{}) 抛 TypeError 走 runner.py:171-179
    的「工具错误回喂给模型」通道。零参数、纯 *args、纯 **kwargs、或参数
    全带默认值的实现都会让 impl(**{}) 静默成功——白烧一次 max_tool_calls
    额度，坏结果被当成正常结果回喂，且不留任何痕迹。

    用显式 raise 而不是 assert：python -O 会把 assert 整条剥掉，那时这道
    校验静默消失。
    """
    for name, fn in impls.items():
        params = inspect.signature(fn).parameters.values()
        has_required = any(
            p.default is inspect.Parameter.empty
            and p.kind
            not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            for p in params
        )
        if not has_required:
            raise ValueError(
                f"工具 {name} 没有任何必填参数。llm/backends/openai.py 在工具"
                "参数解析失败时依赖 impl(**{}) 抛 TypeError 来把错误回喂给模型；"
                "没有必填参数会让那次调用静默成功。请至少保留一个必填参数。"
            )


def build_planning_tools(provider: GeoProvider, city: str):
    def search_poi(query: str) -> dict:
        try:
            hits = provider.search_poi(query, city)
        except ProviderError as e:
            return {"error": str(e), "results": []}
        return {
            "ambiguous": len(hits) > 1,  # 让模型知道名字不唯一，别蒙
            "results": [
                {
                    "id": h.id,
                    "name": h.name,
                    "lat": h.coords.lat,
                    "lng": h.coords.lng,
                    "opening_hours": h.opening_hours,
                }
                for h in hits
            ],
        }

    def route_duration(
        from_lat: float, from_lng: float, to_lat: float, to_lng: float, depart_at: str
    ) -> dict:
        try:
            obs = provider.route(
                LatLng(from_lat, from_lng),
                LatLng(to_lat, to_lng),
                TravelMode.TRANSIT,
                datetime.fromisoformat(depart_at),
            )
        except (ProviderError, ValueError) as e:
            return {"error": str(e)}
        return {
            "duration_min": obs.duration_min,
            "distance_m": obs.distance_m,
            "mode": obs.mode.value,
        }

    specs = [
        {
            "name": "search_poi",
            "description": (
                f"在 {city} 搜索地点，返回候选及其坐标与营业时间。"
                "排行程前先用它确认地点真实存在并拿到坐标。"
                "若 ambiguous 为 true，说明同名地点不止一个，"
                "请在 poi_query 里写得更具体。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "地点名称"}},
                "required": ["query"],
            },
        },
        {
            "name": "route_duration",
            "description": (
                "查两点之间的公共交通耗时（分钟）。"
                "耗时随出发时刻变化，请传实际的出发时间。"
                "排相邻两个活动时用它确认时间留得够。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "from_lat": {"type": "number"},
                    "from_lng": {"type": "number"},
                    "to_lat": {"type": "number"},
                    "to_lng": {"type": "number"},
                    "depart_at": {
                        "type": "string",
                        "description": "ISO 8601，带时区偏移",
                    },
                },
                "required": ["from_lat", "from_lng", "to_lat", "to_lng", "depart_at"],
            },
        },
    ]
    impls = {"search_poi": search_poi, "route_duration": route_duration}
    check_tool_impls(impls)
    return specs, impls
