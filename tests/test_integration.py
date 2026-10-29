"""Integration: HTTP + реальная БД (с откатом) + fakeredis.

Стратегия: сквозные сценарии «снаружи», проверяем поведение API и то,
что оказалось в БД. Покрывает многосущностные кейсы — несколько
ресторанов, несколько курьеров, несколько заказов.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.database.models import (
    Dish, Order, OrderAssignment, StopList,
)
from tests.conftest import create_order, make_ready_order

pytestmark = pytest.mark.integration


# ===========================================================================
# Бизнес-правила создания заказа
# ===========================================================================
async def test_create_order_with_multiple_items_totals(
    integration_client, db_session, client_user, restaurant,
    menu_item_1, menu_item_2,
):
    from sqlalchemy import select
    from backend.database.models import OrderItem

    r = await create_order(
        integration_client,
        client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 2), (menu_item_2.id, 3)],
    )
    assert r.status_code == 201, r.text
    body = r.json()

    # В ответе API — только итог
    assert Decimal(body["total"]) == Decimal("1900.00")
    assert body["status"] == "new"

    # Построчные суммы — через БД
    q = await db_session.execute(
        select(OrderItem).where(OrderItem.order_id == body["id"])
    )
    rows = {it.menu_item_id: it for it in q.scalars().all()}

    assert rows[menu_item_1.id].amount == 2
    assert Decimal(rows[menu_item_1.id].total_cost) == Decimal("1000.00")
    assert rows[menu_item_2.id].amount == 3
    assert Decimal(rows[menu_item_2.id].total_cost) == Decimal("900.00")


async def test_create_order_with_item_from_other_restaurant(
    integration_client, client_user, restaurant, menu_item_other_restaurant,
):
    r = await create_order(
        integration_client,
        client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(menu_item_other_restaurant.id, 1)],
    )
    assert r.status_code == 422
    assert "unavailable" in r.json()["detail"].lower()

async def test_create_order_with_item_in_stop_list(
    integration_client, db_session, client_user, restaurant, menu_item_1,
):
    from backend.database.models import StopList
    from tests.conftest import _make_payload

    payload = _make_payload(
        StopList,
        menu_item_id=menu_item_1.id,
        reason="kitchen overload",
        stopped_at=datetime.now(timezone.utc),
        until=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(StopList(**payload))
    await db_session.flush()

    r = await create_order(
        integration_client,
        client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 1)],
    )
    assert r.status_code == 422
    assert "unavailable" in r.json()["detail"].lower()


# ===========================================================================
# Ресторан: accept → ready
# ===========================================================================
async def _create_order_id(client, client_user, restaurant, menu_item):
    r = await create_order(
        client, client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(menu_item.id, 1)],
    )
    return r.json()["id"]


async def test_restaurant_accepts_order(
    integration_client, db_session, client_user, restaurant, menu_item_1,
):
    order_id = await _create_order_id(
        integration_client, client_user, restaurant, menu_item_1,
    )
    r = await integration_client.post(
        f"/restaurants/{restaurant.id}/orders/{order_id}/accept",
    )
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"

    order = await db_session.get(Order, order_id)
    assert order.status_id != 1  # не "new"


async def test_restaurant_accepts_twice_returns_409(
    integration_client, client_user, restaurant, menu_item_1,
):
    order_id = await _create_order_id(
        integration_client, client_user, restaurant, menu_item_1,
    )
    ok = await integration_client.post(
        f"/restaurants/{restaurant.id}/orders/{order_id}/accept",
    )
    assert ok.status_code == 200

    conflict = await integration_client.post(
        f"/restaurants/{restaurant.id}/orders/{order_id}/accept",
    )
    assert conflict.status_code == 409


async def test_restaurant_marks_order_ready_with_estimate(
    integration_client, db_session, client_user, restaurant, menu_item_1,
):
    order_id = await _create_order_id(
        integration_client, client_user, restaurant, menu_item_1,
    )
    await integration_client.post(
        f"/restaurants/{restaurant.id}/orders/{order_id}/accept",
    )

    eta = datetime.now(timezone.utc) + timedelta(minutes=25)
    r = await integration_client.post(
        f"/restaurants/{restaurant.id}/orders/{order_id}/ready",
        json={"estimated_ready_at": eta.isoformat()},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ready"

    order = await db_session.get(Order, order_id)
    await db_session.refresh(order)
    assert order.estimated_ready_at is not None


# ===========================================================================
# Курьер: accept → pickup → deliver + запреты
# ===========================================================================
async def test_courier_accepts_ready_order(
    integration_client, db_session, client_user, restaurant,
    menu_item_1, courier,
):
    from sqlalchemy import select
    from backend.database.models import OrderAssignment

    order_id = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    r = await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/accept",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "assigned"
    assert body["courier_id"] == courier.id          # int, не str

    q = await db_session.execute(
        select(OrderAssignment).where(
            OrderAssignment.order_id == order_id,
            OrderAssignment.courier_id == courier.id,
        )
    )
    assert q.scalar_one_or_none() is not None


async def test_available_orders_for_courier(
    integration_client, client_user, restaurant,
    menu_item_1, menu_item_2, courier, courier_2,
):
    r_new = await create_order(
        integration_client, client_id=client_user.id,
        restaurant_id=restaurant.id, items=[(menu_item_1.id, 1)],
    )
    r_acc = await create_order(
        integration_client, client_id=client_user.id,
        restaurant_id=restaurant.id, items=[(menu_item_1.id, 1)],
    )
    await integration_client.post(
        f"/restaurants/{restaurant.id}/orders/{r_acc.json()['id']}/accept",
    )
    order_free = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    order_taken = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_2,
    )
    await integration_client.post(
        f"/couriers/{courier_2.id}/orders/{order_taken}/accept",
    )

    r = await integration_client.get(
        f"/couriers/{courier.id}/orders/available",
    )
    assert r.status_code == 200
    raw = r.json()
    # На случай, если API вернёт id/order_id — не завязываемся на одно имя
    ids = {item["id"] if "id" in item else item["order_id"] for item in raw}

    assert order_free in ids
    assert order_taken not in ids
    assert r_new.json()["id"] not in ids
    assert r_acc.json()["id"] not in ids

async def test_full_courier_cycle(
    integration_client, db_session, client_user, restaurant,
    menu_item_1, courier,
):
    order_id = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/accept",
    )

    pick = await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/pickup",
    )
    assert pick.status_code == 200
    assert pick.json()["status"] == "picked_up"

    deliv = await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/deliver",
    )
    assert deliv.status_code == 200
    assert deliv.json()["status"] == "delivered"

    # finished_at — на Order
    order = await db_session.get(Order, order_id)
    await db_session.refresh(order)
    assert order.finished_at is not None

    # picked_up_at — на OrderAssignment
    assignment = (await db_session.execute(
        select(OrderAssignment).where(OrderAssignment.order_id == order_id)
    )).scalar_one()
    assert assignment.picked_up_at is not None


async def test_foreign_courier_cannot_pickup_or_deliver(
    integration_client, client_user, restaurant, menu_item_1,
    courier, courier_2,
):
    order_id = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/accept",
    )

    bad_pick = await integration_client.post(
        f"/couriers/{courier_2.id}/orders/{order_id}/pickup",
    )
    assert bad_pick.status_code == 409
    assert "not assigned to this courier" in bad_pick.json()["detail"]

    bad_deliv = await integration_client.post(
        f"/couriers/{courier_2.id}/orders/{order_id}/deliver",
    )
    assert bad_deliv.status_code == 409


# ===========================================================================
# Меню ресторана
# ===========================================================================
async def test_menu_items_availability_reflects_stop_list(
    integration_client, db_session, restaurant, menu_item_1, menu_item_2,
):
    from backend.database.models import StopList
    from tests.conftest import _make_payload

    payload = _make_payload(
        StopList,
        menu_item_id=menu_item_1.id,
        reason="out of stock",
        stopped_at=datetime.now(timezone.utc),
        until=None,
    )
    db_session.add(StopList(**payload))
    await db_session.flush()

    r = await integration_client.get(
        f"/restaurants/{restaurant.id}/menu-items",
    )
    assert r.status_code == 200
    by_id = {it["id"]: it for it in r.json()}
    assert by_id[menu_item_1.id]["available"] is False
    assert by_id[menu_item_2.id]["available"] is True

async def test_add_menu_item_with_foreign_brand_rejected(
    integration_client, db_session, restaurant, brand, other_brand,
):
    from backend.database.models import Dish
    from tests.conftest import _filtered

    def _dish(brand_id, label):
        payload = _filtered(
            Dish, brand_id=brand_id,
            name=label, title=label, description=None, price=500,
        )
        return Dish(**payload)

    own = _dish(brand.id, "Own")
    foreign = _dish(other_brand.id, "Foreign")
    db_session.add_all([own, foreign])
    await db_session.flush()
    await db_session.refresh(own)
    await db_session.refresh(foreign)

    ok = await integration_client.post(
        f"/restaurants/{restaurant.id}/menu-items",
        json={"dish_id": own.id, "price": 750.0},
    )
    assert ok.status_code == 201, ok.text

    bad = await integration_client.post(
        f"/restaurants/{restaurant.id}/menu-items",
        json={"dish_id": foreign.id, "price": 750.0},
    )
    assert bad.status_code == 422


async def test_update_menu_item_price(
    integration_client, restaurant, menu_item_1,
):
    r = await integration_client.patch(
        f"/restaurants/{restaurant.id}/menu-items/{menu_item_1.id}",
        json={"price": 999.0},
    )
    assert r.status_code == 200
    assert float(r.json()["price"]) == 999.0

    g = await integration_client.get(
        f"/restaurants/{restaurant.id}/menu-items",
    )
    by_id = {it["id"]: it for it in g.json()}
    assert float(by_id[menu_item_1.id]["price"]) == 999.0


# ===========================================================================
# Ближайшие курьеры
# ===========================================================================
async def _set_location(client, courier_id, lon, lat):
    return await client.put(
        f"/couriers/{courier_id}/location",
        json={"longitude": lon, "latitude": lat},
    )


async def test_nearby_couriers_filtered_and_ordered(
    integration_client, restaurant, courier, courier_2, courier_3,
):
    # restaurant ~ (37.6173, 55.7558)
    await _set_location(integration_client, courier.id, 37.6178, 55.7560)   # ~0.7 км
    await _set_location(integration_client, courier_2.id, 37.65, 55.756)    # ~2.8 км
    await _set_location(integration_client, courier_3.id, 38.1, 55.756)     # ~45 км

    r = await integration_client.get(
        f"/restaurants/{restaurant.id}/couriers/nearby",
        params={"radius_km": 5, "limit": 10},
    )
    assert r.status_code == 200

    body = r.json()
    raw_ids = body["courier_ids"] if isinstance(body, dict) else body
    ids = [str(c) for c in raw_ids]

    assert str(courier.id) in ids
    assert str(courier_2.id) in ids
    assert str(courier_3.id) not in ids
    assert ids.index(str(courier.id)) < ids.index(str(courier_2.id))


async def test_courier_location_update_changes_nearby_result(
    integration_client, restaurant, courier,
):
    def _courier_ids(body):
        raw = body["courier_ids"] if isinstance(body, dict) else body
        return [str(c) for c in raw]

    await _set_location(integration_client, courier.id, 38.5, 56.0)
    r1 = await integration_client.get(
        f"/restaurants/{restaurant.id}/couriers/nearby",
        params={"radius_km": 5, "limit": 10},
    )
    assert r1.status_code == 200
    assert str(courier.id) not in _courier_ids(r1.json())

    await _set_location(integration_client, courier.id, 37.6175, 55.7559)
    r2 = await integration_client.get(
        f"/restaurants/{restaurant.id}/couriers/nearby",
        params={"radius_km": 5, "limit": 10},
    )
    assert r2.status_code == 200
    assert str(courier.id) in _courier_ids(r2.json())



## ПРОВЕРКА ЗАКАЗОВ ТУТ
async def test_courier_with_assigned_order_can_take_batch_from_same_restaurant(
    integration_client, client_user, restaurant,
    menu_item_1, menu_item_2, courier,
):
    """Курьер принял заказ из ресторана X (assigned, но не picked_up).
    Приходит второй заказ из X — должен быть виден и принимаем."""
    order_1 = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    r1 = await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_1}/accept",
    )
    assert r1.status_code == 200

    order_2 = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_2,
    )

    # виден в available
    avail = await integration_client.get(
        f"/couriers/{courier.id}/orders/available",
    )
    assert avail.status_code == 200
    seen = {o.get("id", o.get("order_id")) for o in avail.json()}
    assert order_2 in seen, (
        f"Batching разрешён, но {order_2} не появился в available: {seen}"
    )

    # и принимается
    r2 = await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_2}/accept",
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "assigned"


async def test_courier_with_assigned_order_does_not_get_order_from_other_restaurant(
    integration_client, client_user, restaurant, other_restaurant,
    menu_item_1, menu_item_other_restaurant, courier,
):
    """Курьер едет в ресторан X. Заказ из ресторана Y ему не по пути."""
    order_1 = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    assert (await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_1}/accept",
    )).status_code == 200

    order_other = await make_ready_order(
        integration_client, client_user.id, other_restaurant,
        menu_item_other_restaurant,
    )

    avail = await integration_client.get(
        f"/couriers/{courier.id}/orders/available",
    )
    seen = {o.get("id", o.get("order_id")) for o in avail.json()}
    assert order_other not in seen

    r = await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_other}/accept",
    )
    assert r.status_code in (409, 404)


async def test_courier_after_pickup_does_not_see_batch(
    integration_client, client_user, restaurant,
    menu_item_1, menu_item_2, courier,
):
    "После pickup курьер едет к клиенту и возвращаться в ресторан не должен"
    order_1 = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    assert (await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_1}/accept",
    )).status_code == 200
    assert (await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_1}/pickup",
    )).status_code == 200

    order_2 = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_2,
    )
    avail = await integration_client.get(
        f"/couriers/{courier.id}/orders/available",
    )
    seen = {o.get("id", o.get("order_id")) for o in avail.json()}
    assert order_2 not in seen
## КОНЕЦ ПРОВЕРКИ ЗАКАЗОВ 

async def test_courier_cannot_deliver_before_pickup(
    integration_client, client_user, restaurant, menu_item_1, courier,
):
    "Минуя picked_up нельзя перескочить к delivered"
    order_id = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    assert (await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/accept",
    )).status_code == 200

    r = await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/deliver",
    )
    assert r.status_code == 409

async def test_foreign_restaurant_cannot_accept_order(
    integration_client, client_user, restaurant, other_restaurant,
    menu_item_1,
):
    order_id = await _create_order_id(
        integration_client, client_user, restaurant, menu_item_1,
    )
    r = await integration_client.post(
        f"/restaurants/{other_restaurant.id}/orders/{order_id}/accept",
    )
    assert r.status_code in (403, 404)

async def test_order_total_unaffected_by_menu_price_change(
    integration_client, db_session, client_user, restaurant, menu_item_1,
):
    from sqlalchemy import select
    from backend.database.models import OrderItem

    r = await create_order(
        integration_client,
        client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 2)],
    )
    assert r.status_code == 201
    order_id = r.json()["id"]

    # Меняем цену в 10 раз
    await integration_client.patch(
        f"/restaurants/{restaurant.id}/menu-items/{menu_item_1.id}",
        json={"price": 5000.0},
    )

    items = (await db_session.execute(
        select(OrderItem).where(OrderItem.order_id == order_id)
    )).scalars().all()
    assert len(items) == 1
    assert Decimal(items[0].total_cost) == Decimal("1000.00")  # 500 * 2, снимок


async def test_expired_stop_list_does_not_block_order(
    integration_client, db_session, client_user, restaurant, menu_item_1,
):
    from backend.database.models import StopList
    from tests.conftest import _make_payload

    payload = _make_payload(
        StopList,
        menu_item_id=menu_item_1.id,
        reason="was out",
        stopped_at=datetime.now(timezone.utc) - timedelta(hours=2),
        until=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db_session.add(StopList(**payload))
    await db_session.flush()

    r = await create_order(
        integration_client,
        client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 1)],
    )
    assert r.status_code == 201, r.text


async def test_create_order_with_empty_items_rejected(
    integration_client, client_user, restaurant,
):
    r = await integration_client.post("/orders", json={
        "client_id": client_user.id,
        "restaurant_id": restaurant.id,
        "address": "Main street 1",
        "longitude": 37.6, "latitude": 55.7,
        "items": [],
    })
    assert r.status_code == 422


@pytest.mark.parametrize("quantity", [0, -1, -100])
async def test_create_order_with_nonpositive_quantity_rejected(
    integration_client, client_user, restaurant, menu_item_1, quantity,
):
    r = await integration_client.post("/orders", json={
        "client_id": client_user.id,
        "restaurant_id": restaurant.id,
        "address": "Main street 1",
        "longitude": 37.6, "latitude": 55.7,
        "items": [{"menu_item_id": menu_item_1.id, "quantity": quantity}],
    })
    assert r.status_code == 422


async def test_duplicate_external_id_per_provider_rejected(
    db_session, auth_provider,
):
    from sqlalchemy.exc import IntegrityError
    from backend.database.models import Client
    from tests.conftest import _make_payload

    payload = _make_payload(
        Client,
        auth_provider_id=auth_provider.id,
        external_id="dup-001",
    )
    db_session.add(Client(**payload))
    await db_session.flush()

    db_session.add(Client(**payload))
    with pytest.raises(IntegrityError):
        await db_session.flush()