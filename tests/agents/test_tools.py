from tripplan.agents.tools import build_planning_tools
from tripplan.providers.fake import FakeProvider


def _provider():
    return FakeProvider(
        pois={
            "清水寺": [("B001", 34.9949, 135.785)],
            "某某寺": [("B1", 35.0, 135.0), ("B2", 35.5, 135.5)],
        }
    )


def test_exposes_search_and_route_tools():
    specs, impls = build_planning_tools(_provider(), city="京都")
    assert {s["name"] for s in specs} == {"search_poi", "route_duration"}
    assert set(impls) == {"search_poi", "route_duration"}


def test_every_spec_has_an_input_schema():
    specs, _ = build_planning_tools(_provider(), city="京都")
    for s in specs:
        assert s["input_schema"]["type"] == "object"
        assert s["description"]


def test_search_poi_returns_jsonable_summaries():
    _, impls = build_planning_tools(_provider(), city="京都")
    out = impls["search_poi"](query="清水寺")
    assert out["results"][0]["id"] == "B001"
    assert out["results"][0]["lat"] == 34.9949


def test_search_poi_signals_ambiguity_to_the_model():
    """让模型自己知道这个名字不唯一，比让它蒙一个好。"""
    _, impls = build_planning_tools(_provider(), city="京都")
    out = impls["search_poi"](query="某某寺")
    assert out["ambiguous"] is True
    assert len(out["results"]) == 2


def test_search_poi_reports_no_match():
    _, impls = build_planning_tools(_provider(), city="京都")
    assert impls["search_poi"](query="虚构地点")["results"] == []


def test_route_duration_returns_minutes():
    provider = FakeProvider(routes={((35.0, 135.0), (35.1, 135.1)): 40})
    _, impls = build_planning_tools(provider, city="京都")
    out = impls["route_duration"](
        from_lat=35.0,
        from_lng=135.0,
        to_lat=35.1,
        to_lng=135.1,
        depart_at="2026-10-01T11:00:00+09:00",
    )
    assert out["duration_min"] == 40


def test_route_duration_error_is_returned_not_raised():
    provider = FakeProvider(fail_routes={((35.0, 135.0), (35.1, 135.1))})
    _, impls = build_planning_tools(provider, city="京都")
    out = impls["route_duration"](
        from_lat=35.0,
        from_lng=135.0,
        to_lat=35.1,
        to_lng=135.1,
        depart_at="2026-10-01T11:00:00+09:00",
    )
    assert "error" in out


def test_tools_and_resolver_share_the_provider_cache():
    """route() 同时被 LLM 工具与 resolver 调用——这正是两层分开的收益。"""
    provider = _provider()
    _, impls = build_planning_tools(provider, city="京都")
    impls["search_poi"](query="清水寺")
    assert provider.call_log == ["search_poi"]
