from decimal import Decimal

import pytest

from tripplan.models.common import (
    Confidence,
    Field,
    LatLng,
    Money,
    Origin,
    TravelMode,
)


def test_field_defaults_to_no_value():
    f: Field[str] = Field()
    assert f.value is None
    assert f.origin is None
    assert f.confirmed is False
    assert f.rationale == ""


def test_field_carries_origin_and_confirmation_independently():
    """origin 与 confirmed 正交：确认不抹掉「这值本来是模型猜的」。"""
    f = Field(value="京都", origin=Origin.MODEL, rationale="从「关西」推断")
    confirmed = f.confirm()
    assert confirmed.confirmed is True
    assert confirmed.origin is Origin.MODEL  # ★ 来源不变
    assert confirmed.value == "京都"
    assert f.confirmed is False  # 原对象不可变


def test_field_is_frozen():
    f = Field(value=1, origin=Origin.USER)
    with pytest.raises(Exception):
        f.value = 2


def test_money_rejects_float_amount():
    with pytest.raises(TypeError):
        Money(
            amount=12.5, currency="CNY", confidence=Confidence.ESTIMATED, source="llm"
        )


def test_money_accepts_decimal():
    m = Money(
        amount=Decimal("1240.00"),
        currency="CNY",
        confidence=Confidence.VERIFIED,
        source="amap:ticket",
    )
    assert m.amount == Decimal("1240.00")


def test_latlng_and_travelmode_exist():
    p = LatLng(lat=35.0, lng=135.7)
    assert (p.lat, p.lng) == (35.0, 135.7)
    assert TravelMode.TRANSIT.value == "TRANSIT"
