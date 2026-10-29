"""HTTP-контрактные тесты: эндпоинты проходят, зависимости замоканы.

Стратегия: реальный FastAPI + Pydantic, но `get_db_session` подменён
AsyncMock'ом. Проверяет форму ответа, коды и порядок обращений к сессии.
Не поднимает ни БД, ни Redis.
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from backend.api import couriers, restaurants
from backend.api.statuses import READY
from backend.database.models import (
    Order, OrderItem, OrderStatus,
)
from backend.database.session import get_db_session
from backend.main import app

pytestmark = pytest.mark.contract


# ---------------------------------------------------------------------------
# Локальные фикстуры и хелперы
# ---------------------------------------------------------------------------
@pytest.fixture
async def contract_client():
    session = AsyncMock()
    session.add = Mock()

    async def override():
        yield session

    app.dependency_overrides[get_db_session] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            yield client, session
    finally:
        app.dependency_overrides.clear()


def order_payload(*, client_id=7, restaurant_id=11,
                  longitude=37.6, latitude=55.7,
                  items=None, address="Main street 1"):
    return {
        "client_id": client_id,
        "restaurant_id": restaurant_id,
        "address": address,
        "longitude": longitude,
        "latitude": latitude,
        "items": items or [{"menu_item_id": 31, "quantity": 1}],
    }


def _ready_order(**kw):
    base = dict(
        id=42, client_id=7, status_id=3, restaurant_id=11,
        address="Main street 1",
        longitude=Decimal("37.6"), latitude=Decimal("55.7"),
    )
    base.update(kw)
    return Order(**base)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
async def test_health_check(contract_client):
    client, _ = contract_client
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Создание заказа — успех и отказы
# ---------------------------------------------------------------------------
async def test_create_order_saves_items_and_total(contract_client):
    client, session = contract_client
    menu_item = SimpleNamespace(id=31, price=Decimal("12.50"))

    session.get.side_effect = [
        SimpleNamespace(id=7),   # Client
        SimpleNamespace(id=11),  # Restaurant
    ]
    session.scalars.return_value = SimpleNamespace(all=lambda: [menu_item])
    session.scalar.return_value = 4  # id статуса "new"

    def assign_order_id():
        created = next(
            c.args[0] for c in session.add.call_args_list
            if isinstance(c.args[0], Order)
        )
        created.id = 100

    session.flush.side_effect = assign_order_id

    response = await client.post(
        "/orders",
        json=order_payload(items=[{"menu_item_id": 31, "quantity": 2}]),
    )

    assert response.status_code == 201
    assert response.json() == {
        "id": 100, "status": "new",
        "restaurant_id": 11, "total": "25.00",
    }

    added = [c.args[0] for c in session.add.call_args_list
             if isinstance(c.args[0], OrderItem)]
    assert len(added) == 1
    assert added[0].amount == 2
    assert added[0].total_cost == Decimal("25.00")
    session.commit.assert_awaited_once()


async def test_create_order_rejects_duplicate_menu_items(contract_client):
    client, session = contract_client
    session.get.side_effect = [SimpleNamespace(id=7), SimpleNamespace(id=11)]

    response = await client.post(
        "/orders",
        json=order_payload(items=[
            {"menu_item_id": 31, "quantity": 1},
            {"menu_item_id": 31, "quantity": 2},
        ]),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Duplicate menu_item_id"
    session.commit.assert_not_awaited()


async def test_create_order_unknown_client(contract_client):
    client, session = contract_client
    session.get.side_effect = [None]

    response = await client.post("/orders", json=order_payload())

    assert response.status_code == 404
    assert "Client not found" in response.json()["detail"]
    session.commit.assert_not_awaited()


async def test_create_order_unknown_restaurant(contract_client):
    client, session = contract_client
    session.get.side_effect = [SimpleNamespace(id=7), None]

    response = await client.post("/orders", json=order_payload())

    assert response.status_code == 404
    assert "Restaurant not found" in response.json()["detail"]
    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Валидация запроса до похода в БД
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("longitude", 181),
        ("longitude", -181),
        ("latitude", 91),
        ("latitude", -91),
        ("client_id", 0),
        ("client_id", -1),
    ],
)
async def test_create_order_validates_request(contract_client, field, value):
    client, session = contract_client
    payload = order_payload()
    payload[field] = value

    response = await client.post("/orders", json=payload)

    assert response.status_code == 422
    session.get.assert_not_awaited()
    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Accept-эндпоинты (helpers замоканы)
# ---------------------------------------------------------------------------
async def test_restaurant_accepts_new_order(monkeypatch):
    session = AsyncMock()
    session.scalar.return_value = 5
    session.get.return_value = OrderStatus(id=1, name="new")
    order = _ready_order(status_id=1)

    monkeypatch.setattr(restaurants, "require_restaurant", AsyncMock())
    monkeypatch.setattr(
        restaurants, "get_restaurant_order", AsyncMock(return_value=order),
    )

    result = await restaurants.accept_order(11, 42, session)

    assert result == {"order_id": 42, "status": "accepted"}
    assert order.status_id == 5
    session.commit.assert_awaited_once()


async def test_restaurant_accept_twice_returns_409(monkeypatch):
    session = AsyncMock()
    session.get.return_value = OrderStatus(id=2, name="accepted")
    order = _ready_order(status_id=2)

    monkeypatch.setattr(restaurants, "require_restaurant", AsyncMock())
    monkeypatch.setattr(
        restaurants, "get_restaurant_order", AsyncMock(return_value=order),
    )

    with pytest.raises(HTTPException) as err:
        await restaurants.accept_order(11, 42, session)

    assert err.value.status_code == 409
    session.commit.assert_not_awaited()


async def test_courier_accepts_ready_order(monkeypatch):
    session = AsyncMock()
    session.add = Mock()
    session.scalar.side_effect = [None, 6]
    session.get.return_value = OrderStatus(id=3, name=READY)
    order = _ready_order(status_id=3)

    monkeypatch.setattr(couriers, "require_courier", AsyncMock())
    monkeypatch.setattr(
        couriers, "get_order_for_update", AsyncMock(return_value=order),
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
    session.scalar.return_value = 99  # уже существующее назначение
    session.get.return_value = OrderStatus(id=3, name=READY)
    order = _ready_order(status_id=3)

    monkeypatch.setattr(couriers, "require_courier", AsyncMock())
    monkeypatch.setattr(
        couriers, "get_order_for_update", AsyncMock(return_value=order),
    )

    with pytest.raises(HTTPException) as err:
        await couriers.accept_delivery(9, 42, session)

    assert err.value.status_code == 409
    session.commit.assert_not_awaited()