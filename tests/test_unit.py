"""Unit-тесты чистых функций: логика статусов и Redis-обёртка.

Стратегия: без HTTP, без БД. Только прямые вызовы функций с моками.
"""
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from backend.api.statuses import set_order_status
from backend.courier_locations import (
    COURIER_LOCATIONS_KEY,
    find_nearby_couriers,
    save_courier_location,
)
from backend.database.models import Order, OrderStatus

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Локальные фикстуры
# ---------------------------------------------------------------------------
@pytest.fixture
def session_mock():
    """AsyncSession с синхронным .add (как в реальном API)."""
    session = AsyncMock()
    session.add = Mock()
    return session


@pytest.fixture
def redis_mock(monkeypatch):
    redis = AsyncMock()
    monkeypatch.setattr("backend.courier_locations.redis_client", redis)
    return redis


# ---------------------------------------------------------------------------
# Установка статуса
# ---------------------------------------------------------------------------
async def test_set_order_status_updates_status_id_only(session_mock):
    session_mock.scalar.return_value = 8  # id уже существующего "accepted"
    order = Order(
        id=42, client_id=7, status_id=3, restaurant_id=11,
        address="Main street 1",
        longitude=Decimal("37.6"), latitude=Decimal("55.7"),
    )

    await set_order_status(session_mock, order, "accepted")

    assert order.id == 42
    assert order.status_id == 8
    assert order.client_id == 7
    assert order.restaurant_id == 11


async def test_status_is_created_when_not_found(session_mock):
    session_mock.scalar.return_value = None
    created = {}

    def add(value):
        if isinstance(value, OrderStatus):
            created["status"] = value

    session_mock.add.side_effect = add
    session_mock.flush.side_effect = lambda: setattr(created["status"], "id", 8)

    await set_order_status(
        session_mock,
        Order(
            id=42, client_id=7, status_id=3, restaurant_id=11,
            address="Main street 1",
            longitude=Decimal("37.6"), latitude=Decimal("55.7"),
        ),
        "accepted",
    )

    assert isinstance(created["status"], OrderStatus)
    assert created["status"].name == "accepted"
    session_mock.add.assert_called_with(created["status"])


# ---------------------------------------------------------------------------
# Redis-обёртка
# ---------------------------------------------------------------------------
async def test_save_and_search_courier_location(redis_mock):
    redis_mock.geosearch.return_value = ["15", "9"]

    await save_courier_location(15, 37.6, 55.7)
    result = await find_nearby_couriers(37.6, 55.7, 3, limit=2)

    # фактический вызов: geoadd(KEY, (lon, lat, member))
    redis_mock.geoadd.assert_awaited_once_with(
        COURIER_LOCATIONS_KEY, (37.6, 55.7, "15"),
    )
    redis_mock.geosearch.assert_awaited_once_with(
        COURIER_LOCATIONS_KEY,
        longitude=37.6, latitude=55.7, radius=3,
        unit="km", sort="ASC", count=2,
    )
    assert result == ["15", "9"]
    

@pytest.mark.parametrize(
    ("radius_km", "limit"),
    [(0, 10), (-1, 10), (1, 0)],
)
async def test_nearby_couriers_rejects_invalid_search(radius_km, limit):
    with pytest.raises(ValueError):
        await find_nearby_couriers(37.6, 55.7, radius_km, limit)