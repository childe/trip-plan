import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from tripplan.models.common import LatLng, TravelMode
from tripplan.providers.amap import AmapProvider
from tripplan.providers.base import ProviderError
from tripplan.providers.cache import DiskCache

JST = timezone(timedelta(hours=9))
WHEN = datetime(2026, 10, 1, 11, 0, tzinfo=JST)

POI_OK = {
    "status": "1",
    "pois": [
        {
            "id": "B001",
            "name": "清水寺",
            "location": "135.785,34.9949",
            "business": {"opentime_week": "06:00-18:00"},
        }
    ],
}
ROUTE_OK = {
    "status": "1",
    "route": {
        "transits": [
            {
                "duration": "2400",
                "distance": "4200",
                "segments": [
                    {"bus": {"buslines": [{"polyline": "135.7,34.9;135.8,35.0"}]}}
                ],
            }
        ]
    },
}


def _provider(handler, tmp_path, **kw):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return AmapProvider(
        key="test-key", cache=DiskCache(tmp_path, ttl_days=7), http=client, **kw
    )


def test_search_poi_parses_response(tmp_path):
    def handler(request):
        assert "place/text" in str(request.url)
        assert "key=test-key" in str(request.url)
        return httpx.Response(200, json=POI_OK)

    got = _provider(handler, tmp_path).search_poi("清水寺", "京都")
    assert got[0].id == "B001"
    assert got[0].coords == LatLng(34.9949, 135.785)
    assert got[0].opening_hours == "06:00-18:00"
    assert got[0].source.startswith("amap")
    assert got[0].fetched_at.tzinfo is not None


def test_search_poi_caches_by_query_and_city(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=POI_OK)

    p = _provider(handler, tmp_path)
    p.search_poi("清水寺", "京都")
    p.search_poi("清水寺", "京都")
    assert len(calls) == 1  # 第二次命中缓存


def test_search_poi_different_city_is_a_different_key(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=POI_OK)

    p = _provider(handler, tmp_path)
    p.search_poi("清水寺", "京都")
    p.search_poi("清水寺", "大阪")
    assert len(calls) == 2


def test_route_parses_duration_distance_and_polyline(tmp_path):
    def handler(request):
        return httpx.Response(200, json=ROUTE_OK)

    obs = _provider(handler, tmp_path).route(
        LatLng(34.9949, 135.785), LatLng(35.0036, 135.7786), TravelMode.TRANSIT, WHEN
    )
    assert obs.duration_min == 40  # 2400 秒
    assert obs.distance_m == 4200
    assert obs.polyline
    assert obs.mode is TravelMode.TRANSIT


def test_route_cache_key_includes_departure_hour(tmp_path):
    """早高峰与午间耗时不同，缓存键必须区分出发时段。"""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=ROUTE_OK)

    p = _provider(handler, tmp_path)
    a, b = LatLng(34.9949, 135.785), LatLng(35.0036, 135.7786)
    p.route(a, b, TravelMode.TRANSIT, WHEN)
    p.route(a, b, TravelMode.TRANSIT, WHEN.replace(hour=8))
    assert len(calls) == 2
    p.route(a, b, TravelMode.TRANSIT, WHEN.replace(minute=30))
    assert len(calls) == 2  # 同一小时内共用


def test_api_error_status_becomes_provider_error(tmp_path):
    def handler(request):
        return httpx.Response(
            200, json={"status": "0", "info": "DAILY_QUERY_OVER_LIMIT"}
        )

    with pytest.raises(ProviderError, match="DAILY_QUERY_OVER_LIMIT"):
        _provider(handler, tmp_path).search_poi("清水寺", "京都")


def test_http_error_becomes_provider_error(tmp_path):
    def handler(request):
        return httpx.Response(500, text="boom")

    with pytest.raises(ProviderError):
        _provider(handler, tmp_path).search_poi("清水寺", "京都")


def test_network_failure_becomes_provider_error(tmp_path):
    def handler(request):
        raise httpx.ConnectError("no network")

    with pytest.raises(ProviderError):
        _provider(handler, tmp_path).search_poi("清水寺", "京都")


def test_no_route_found_raises_provider_error(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"status": "1", "route": {"transits": []}})

    with pytest.raises(ProviderError, match="没有可用路线"):
        _provider(handler, tmp_path).route(
            LatLng(34.9949, 135.785),
            LatLng(35.0036, 135.7786),
            TravelMode.TRANSIT,
            WHEN,
        )


def test_static_map_returns_bytes_and_is_cached(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=b"\x89PNG-fake")

    p = _provider(handler, tmp_path)
    pts = [LatLng(34.9949, 135.785), LatLng(35.0036, 135.7786)]
    assert p.static_map(pts, polyline="135.7,34.9;135.8,35.0") == b"\x89PNG-fake"
    p.static_map(pts, polyline="135.7,34.9;135.8,35.0")
    assert len(calls) == 1


def test_timezone_of_maps_known_cities(tmp_path):
    def handler(request):
        raise AssertionError("时区查询不该走网络")

    assert _provider(handler, tmp_path).timezone_of("京都") == "Asia/Tokyo"


def test_timezone_of_unknown_city_raises(tmp_path):
    def handler(request):
        raise AssertionError("不该走网络")

    with pytest.raises(ProviderError):
        _provider(handler, tmp_path).timezone_of("虚构城")


@pytest.mark.slow
def test_real_amap_smoke(tmp_path):
    """真实连通性。需要 AMAP_KEY 环境变量，默认不跑。"""
    import os

    key = os.environ.get("AMAP_KEY")
    if not key:
        pytest.skip("未设置 AMAP_KEY")
    p = AmapProvider(key=key, cache=DiskCache(tmp_path, ttl_days=1))
    hits = p.search_poi("外滩", "上海")
    assert hits and hits[0].coords.lat > 30
