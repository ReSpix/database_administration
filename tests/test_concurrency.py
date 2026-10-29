"""Concurrency: реальная БД, две независимые транзакции, гонка."""
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from backend.api import couriers
from backend.api.statuses import set_order_status
from backend.database.models import (
    Brand, Client, Courier, Order, OrderAssignment, Restaurant,
)
from backend.database.session import engine as _app_engine, get_db_session
from tests.conftest import _filtered

from httpx import ASGITransport, AsyncClient
from backend.main import app as _app

pytestmark = pytest.mark.concurrency

_engine = create_async_engine(_app_engine.url, poolclass=NullPool)


def _session():
    return AsyncSession(bind=_engine, expire_on_commit=False)


@pytest.fixture
async def seeded_order():
    """Создаёт бренд, ресторан, AuthProvider + клиента, двух курьеров,
    OrderStatus "new" и готовый заказ. Всё реально коммитится,
    в finally — очистка."""
    from backend.database.models import AuthProvider, OrderStatus
    from tests.conftest import _make_payload

    async with _session() as s:
        provider = AuthProvider(**_make_payload(AuthProvider, name="test"))
        s.add(provider)
        await s.flush()

        brand = Brand(**_make_payload(Brand, name="R-Brand", description=None))
        s.add(brand)
        await s.flush()

        restaurant = Restaurant(**_make_payload(
            Restaurant,
            brand_id=brand.id, address="Main 1",
            longitude=37.6, latitude=55.7,
        ))
        client = Client(**_make_payload(
            Client,
            auth_provider_id=provider.id,
            external_id="test-client",
        ))
        courier_a = Courier(**_make_payload(Courier, name="A"))
        courier_b = Courier(**_make_payload(Courier, name="B"))

        # статус "new" — обязательный FK для Order; на чистой БД его нет
        status_new = OrderStatus(**_make_payload(OrderStatus, name="new"))
        s.add(status_new)
        await s.flush()

        s.add_all([restaurant, client, courier_a, courier_b])
        await s.flush()

        order = Order(**_make_payload(
            Order,
            client_id=client.id, restaurant_id=restaurant.id,
            status_id=status_new.id,               # ← реальный id, не 1
            address="Main 1", longitude=37.6, latitude=55.7,
        ))
        s.add(order)
        await s.flush()
        await set_order_status(s, order, "ready")  # создаст "ready", если нет
        await s.commit()

        ids = {
            "provider": provider.id,
            "brand": brand.id,
            "restaurant": restaurant.id,
            "client": client.id,
            "courier_a": courier_a.id,
            "courier_b": courier_b.id,
            "order": order.id,
            "status_new": status_new.id,
        }

    try:
        yield ids
    finally:
        async with _session() as s:
            await s.execute(delete(OrderAssignment).where(
                OrderAssignment.order_id == ids["order"],
            ))
            await s.execute(delete(Order).where(Order.id == ids["order"]))
            await s.execute(delete(Courier).where(
                Courier.id.in_([ids["courier_a"], ids["courier_b"]]),
            ))
            await s.execute(delete(Client).where(Client.id == ids["client"]))
            await s.execute(delete(Restaurant).where(
                Restaurant.id == ids["restaurant"],
            ))
            await s.execute(delete(Brand).where(Brand.id == ids["brand"]))
            await s.execute(delete(AuthProvider).where(
                AuthProvider.id == ids["provider"],
            ))
            # Статусы "new"/"ready" трогать не нужно: их могут использовать
            # другие тесты в этой же сессии, а TRUNCATE в начале сессии
            # всё равно всё снесёт.
            await s.commit()
        await _engine.dispose()


async def _try_accept(courier_id, order_id):
    async with _session() as s:
        try:
            return "ok", await couriers.accept_delivery(courier_id, order_id, s)
        except HTTPException as e:
            return "err", e


async def test_concurrent_accept_only_one_wins(seeded_order):
    r1, r2 = await asyncio.gather(
        _try_accept(seeded_order["courier_a"], seeded_order["order"]),
        _try_accept(seeded_order["courier_b"], seeded_order["order"]),
    )

    outcomes = [r1, r2]
    successes = [o for o in outcomes if o[0] == "ok"]
    errors = [o for o in outcomes if o[0] == "err"]

    assert len(successes) == 1, (
        f"Ожидался ровно один успех, получено: {outcomes}. "
        "Вероятно, в accept_delivery отсутствует блокировка строки "
        "(SELECT ... FOR UPDATE) — это баг."
    )
    assert len(errors) == 1
    assert errors[0][1].status_code == 409

    async with _session() as s:
        rows = (await s.execute(
            select(OrderAssignment).where(
                OrderAssignment.order_id == seeded_order["order"],
            )
        )).scalars().all()
        assert len(rows) == 1

@pytest.fixture
async def concurrent_client():
    """Каждый HTTP-запрос получает свою транзакцию.

    В отличие от `integration_client` (общий savepoint-rollback),
    здесь сессии независимы — это необходимо для настоящих гонок.
    """
    async def override():
        async with _session() as s:
            yield s

    _app.dependency_overrides[get_db_session] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=_app), base_url="http://test",
        ) as c:
            yield c
    finally:
        _app.dependency_overrides.clear()


@pytest.fixture
async def assigned_order(seeded_order, concurrent_client):
    """seeded_order в состоянии ready → accepted курьером A → assigned."""
    r = await concurrent_client.post(
        f"/couriers/{seeded_order['courier_a']}"
        f"/orders/{seeded_order['order']}/accept",
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assigned"
    return seeded_order


async def test_concurrent_pickup_only_one_wins(
    assigned_order, concurrent_client,
):
    """Двойной тап «picked up» / повтор при ретрае: ровно один 200,
    второй — 409. Ловит отсутствие блокировки в pickup-переходе
    (аналог уже покрытого race на accept)."""
    courier_id = assigned_order["courier_a"]
    order_id = assigned_order["order"]
    url = f"/couriers/{courier_id}/orders/{order_id}/pickup"

    r1, r2 = await asyncio.gather(
        concurrent_client.post(url),
        concurrent_client.post(url),
    )
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], (
        f"Ожидалось ровно [200, 409], получено {codes}. "
        f"body1={r1.text}, body2={r2.text}. "
        "Вероятно, pickup не блокирует строку — двойной picked_up_at."
    )

    # Финальное состояние: ровно одна запись assignment с picked_up_at
    async with _session() as s:
        from sqlalchemy import select as _select
        rows = (await s.execute(
            _select(OrderAssignment).where(
                OrderAssignment.order_id == order_id,
            )
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].picked_up_at is not None