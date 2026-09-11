from datetime import date
from decimal import Decimal

from tripplan.models.common import Confidence, Money
from tripplan.models.itinerary import Category
from tripplan.maps import fetch_day_maps
from tripplan.providers.fake import FakeProvider

D1 = date(2026, 10, 1)


def _itin(mk):
    return mk.itin(
        [
            mk.day(
                "d1",
                D1,
                [
                    mk.act("d1a1", "d1", "09:00", "11:00", query="清水寺"),
                    mk.act(
                        "d1a2",
                        "d1",
                        "12:00",
                        "13:00",
                        query="某食堂",
                        category=Category.MEAL,
                        cost=Money(Decimal("1500"), "JPY", Confidence.ESTIMATED, "llm"),
                    ),
                ],
            )
        ]
    )


def test_fetch_day_maps_returns_one_image_per_day(mk):
    facts = mk.facts(
        poi_by_activity={"d1a1": mk.resolved("B001"), "d1a2": mk.resolved("B002")}
    )
    maps = fetch_day_maps(_itin(mk), facts, FakeProvider())
    assert set(maps) == {"d1"}
    assert maps["d1"].startswith(b"\x89PNG")


def test_fetch_day_maps_skips_days_without_resolved_coords(mk):
    maps = fetch_day_maps(_itin(mk), mk.facts(), FakeProvider())
    assert maps == {}


def test_fetch_day_maps_survives_provider_failure(mk):
    class Broken(FakeProvider):
        def static_map(self, points, polyline=None):
            from tripplan.providers.base import ProviderError

            raise ProviderError("限流")

    facts = mk.facts(poi_by_activity={"d1a1": mk.resolved("B001")})
    assert fetch_day_maps(_itin(mk), facts, Broken()) == {}
