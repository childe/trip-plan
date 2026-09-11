from datetime import datetime, timedelta, timezone

import pytest

from tripplan.models.common import LatLng, TravelMode
from tripplan.providers.base import ProviderError
from tripplan.providers.fake import FakeProvider

JST = timezone(timedelta(hours=9))
WHEN = datetime(2026, 10, 1, 11, 0, tzinfo=JST)


def test_search_returns_configured_pois():
    p = FakeProvider(pois={"清水寺": [("B001", 34.99, 135.78)]})
    got = p.search_poi("清水寺", city="京都")
    assert [x.id for x in got] == ["B001"]
    assert got[0].coords == LatLng(34.99, 135.78)


def test_search_returns_empty_for_unknown_query():
    assert FakeProvider().search_poi("不存在", city="京都") == []


def test_search_can_return_multiple_for_ambiguity_tests():
    p = FakeProvider(pois={"某某寺": [("B1", 35.0, 135.0), ("B2", 35.1, 135.1)]})
    assert len(p.search_poi("某某寺", city="京都")) == 2


def test_route_is_deterministic_for_same_inputs():
    p = FakeProvider()
    a, b = LatLng(35.0, 135.0), LatLng(35.1, 135.1)
    first = p.route(a, b, TravelMode.TRANSIT, WHEN)
    second = p.route(a, b, TravelMode.TRANSIT, WHEN)
    assert first == second


def test_route_can_be_scripted_per_pair():
    p = FakeProvider(routes={((35.0, 135.0), (35.1, 135.1)): 40})
    obs = p.route(LatLng(35.0, 135.0), LatLng(35.1, 135.1), TravelMode.TRANSIT, WHEN)
    assert obs.duration_min == 40
    assert obs.polyline


def test_route_failure_can_be_scripted():
    p = FakeProvider(fail_routes={((35.0, 135.0), (35.1, 135.1))})
    with pytest.raises(ProviderError):
        p.route(LatLng(35.0, 135.0), LatLng(35.1, 135.1), TravelMode.TRANSIT, WHEN)


def test_static_map_returns_png_bytes():
    data = FakeProvider().static_map([LatLng(35.0, 135.0)], polyline=None)
    assert data.startswith(b"\x89PNG")


def test_timezone_of_known_city():
    assert FakeProvider().timezone_of("京都") == "Asia/Tokyo"


def test_timezone_of_unknown_city_raises():
    with pytest.raises(ProviderError):
        FakeProvider().timezone_of("虚构城")


def test_call_log_lets_tests_assert_no_redundant_lookups():
    p = FakeProvider()
    a, b = LatLng(35.0, 135.0), LatLng(35.1, 135.1)
    p.route(a, b, TravelMode.TRANSIT, WHEN)
    p.route(a, b, TravelMode.TRANSIT, WHEN)
    assert p.call_log.count("route") == 2
