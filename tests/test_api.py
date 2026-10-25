from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from backend.api import couriers, restaurants
from backend.api.statuses import READY, set_order_status
from backend.courier_locations import (
    COURIER_LOCATIONS_KEY,
    find_nearby_couriers,
    save_courier_location,
)
from backend.database.models import Order, OrderItem, OrderStatus
from backend.database.session import get_db_session
from backend.main import app


@pytest.fixture
async def client():
    session = AsyncMock()
    session.add = Mock()

    async def override_session():
        yield session

    app.dependency_overrides[get_db_session] = override_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as test_client:
            yield test_client, session
    finally:
        app.dependency_overrides.clear()


async def test_health_check(client):
    test_client, _ = client

    response = await test_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_create_order_saves_items_and_total(client):
    test_client, session = client
    menu_item = SimpleNamespace(id=31, price=Decimal("12.50"))
    session.get.side_effect = [
        SimpleNamespace(id=7),
        SimpleNamespace(id=11),
    ]
    session.scalars.return_value = SimpleNamespace(all=lambda: [menu_item])
    session.scalar.return_value = 4

    def assign_order_id():
        created_order = next(
            call.args[0]
            for call in session.add.call_args_list
            if isinstance(call.args[0], Order)
        )
        created_order.id = 100

    session.flush.side_effect = assign_order_id

    response = await test_client.post(
        "/orders",
        json={
            "client_id": 7,
            "restaurant_id": 11,
            "address": "Main street 1",
            "longitude": 37.6,
            "latitude": 55.7,
            "items": [{"menu_item_id": 31, "quantity": 2}],
        },
    )

    assert response.status_code == 201
    assert response.json() == {
        "id": 100,
        "status": "new",
        "restaurant_id": 11,
        "total": "25.00",
    }
    added_items = [
        call.args[0]
        for call in session.add.call_args_list
        if isinstance(call.args[0], OrderItem)
    ]
    assert len(added_items) == 1
    assert added_items[0].amount == 2
    assert added_items[0].total_cost == Decimal("25.00")
    session.commit.assert_awaited_once()


async def test_create_order_rejects_duplicate_menu_items(client):
    test_client, session = client
    session.get.side_effect = [
        SimpleNamespace(id=7),
        SimpleNamespace(id=11),
    ]

    response = await test_client.post(
        "/orders",
        json={
            "client_id": 7,
            "restaurant_id": 11,
            "address": "Main street 1",
            "longitude": 37.6,
            "latitude": 55.7,
            "items": [
                {"menu_item_id": 31, "quantity": 1},
                {"menu_item_id": 31, "quantity": 2},
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Duplicate menu_item_id"
    session.commit.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("longitude", 181),
        ("latitude", -91),
        ("client_id", 0),
    ],
)
async def test_create_order_validates_request(client, field, value):
    test_client, session = client
    payload = {
        "client_id": 7,
        "restaurant_id": 11,
        "address": "Main street 1",
        "longitude": 37.6,
        "latitude": 55.7,
        "items": [{"menu_item_id": 31, "quantity": 1}],
    }
    payload[field] = value

    response = await test_client.post("/orders", json=payload)

    assert response.status_code == 422
    session.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_order_status_updates_status_id_only():
    session = AsyncMock()
    session.scalar.return_value = 8
    order = Order(
        id=42,
        client_id=7,
        status_id=3,
        restaurant_id=11,
        address="Main street 1",
        longitude=Decimal("37.6"),
        latitude=Decimal("55.7"),
    )

    await set_order_status(session, order, "accepted")

    assert order.id == 42
    assert order.status_id == 8


async def test_restaurant_accepts_new_order(monkeypatch):
    session = AsyncMock()
    session.scalar.return_value = 5
    session.get.return_value = OrderStatus(id=1, name="new")
    order = Order(
        id=42,
        client_id=7,
        status_id=1,
        restaurant_id=11,
        address="Main street 1",
        longitude=Decimal("37.6"),
        latitude=Decimal("55.7"),
    )
    monkeypatch.setattr(restaurants, "require_restaurant", AsyncMock())
    monkeypatch.setattr(
        restaurants,
        "get_restaurant_order",
        AsyncMock(return_value=order),
    )

    result = await restaurants.accept_order(11, 42, session)

    assert result == {"order_id": 42, "status": "accepted"}
    assert order.status_id == 5
    session.commit.assert_awaited_once()


async def test_courier_accepts_ready_order(monkeypatch):
    session = AsyncMock()
    session.add = Mock()
    session.scalar.side_effect = [None, 6]
    session.get.return_value = OrderStatus(id=3, name=READY)
    order = Order(
        id=42,
        client_id=7,
        status_id=3,
        restaurant_id=11,
        address="Main street 1",
        longitude=Decimal("37.6"),
        latitude=Decimal("55.7"),
    )
    monkeypatch.setattr(couriers, "require_courier", AsyncMock())
    monkeypatch.setattr(
        couriers,
        "get_order_for_update",
        AsyncMock(return_value=order),
    )

    result = await couriers.accept_delivery(9, 42, session)

    assert result == {"order_id": 42, "courier_id": 9, "status": "assigned"}
    assert order.status_id == 6
    assignment = session.add.call_args.args[0]
    assert assignment.order_id == 42
    assert assignment.courier_id == 9
    session.commit.assert_awaited_once()


async def test_courier_cannot_accept_already_assigned_order(monkeypatch):
    session = AsyncMock()
    session.scalar.return_value = 99
    session.get.return_value = OrderStatus(id=3, name=READY)
    order = Order(
        id=42,
        client_id=7,
        status_id=3,
        restaurant_id=11,
        address="Main street 1",
        longitude=Decimal("37.6"),
        latitude=Decimal("55.7"),
    )
    monkeypatch.setattr(couriers, "require_courier", AsyncMock())
    monkeypatch.setattr(
        couriers,
        "get_order_for_update",
        AsyncMock(return_value=order),
    )

    with pytest.raises(HTTPException) as error:
        await couriers.accept_delivery(9, 42, session)

    assert error.value.status_code == 409
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_is_created_when_not_found():
    session = AsyncMock()
    session.add = Mock()
    session.scalar.return_value = None
    new_status = None

    def add(value):
        nonlocal new_status
        if isinstance(value, OrderStatus):
            new_status = value

    session.add.side_effect = add
    session.flush.side_effect = lambda: setattr(new_status, "id", 8)

    await set_order_status(
        session,
        Order(
            id=42,
            client_id=7,
            status_id=3,
            restaurant_id=11,
            address="Main street 1",
            longitude=Decimal("37.6"),
            latitude=Decimal("55.7"),
        ),
        "accepted",
    )

    assert isinstance(new_status, OrderStatus)
    assert new_status.name == "accepted"


@pytest.mark.asyncio
async def test_save_and_search_courier_location(monkeypatch):
    redis = AsyncMock()
    redis.geosearch.return_value = ["15", "9"]
    monkeypatch.setattr("backend.courier_locations.redis_client", redis)

    await save_courier_location(15, 37.6, 55.7)
    result = await find_nearby_couriers(37.6, 55.7, 3, limit=2)

    redis.geoadd.assert_awaited_once_with(
        COURIER_LOCATIONS_KEY,
        {"15": (37.6, 55.7)},
    )
    redis.geosearch.assert_awaited_once_with(
        COURIER_LOCATIONS_KEY,
        longitude=37.6,
        latitude=55.7,
        radius=3,
        unit="km",
        sort="ASC",
        count=2,
    )
    assert result == ["15", "9"]


@pytest.mark.parametrize(
    ("radius_km", "limit"),
    [(0, 10), (-1, 10), (1, 0)],
)
@pytest.mark.asyncio
async def test_nearby_couriers_rejects_invalid_search(radius_km, limit):
    with pytest.raises(ValueError):
        await find_nearby_couriers(37.6, 55.7, radius_km, limit)
