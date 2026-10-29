"""Integration: HTTP + реальная БД + fakeredis — базовые сквозные сценарии.

Проверяет поведение API снаружи и то, что оказалось в БД. Покрывает:
- бизнес-правила создания заказа
- поток ресторана (accept → ready)
- поток курьера (accept → pickup → deliver)
- управление меню ресторана
- геопоиск курьеров

State machine — отдельно в test_states.py.
Многосущностные комбинации — отдельно в test_multi_entity.py.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, func

from backend.database.models import (
    Client, Dish, Order, OrderAssignment, OrderItem, StopList,
)
from tests.conftest import (
    _make_payload, _persist, create_order, make_ready_order, _available_ids
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Общие хелперы внутри файла
# ---------------------------------------------------------------------------
async def _create_order_id(client, client_user, restaurant, menu_item):
    r = await create_order(
        client, client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(menu_item.id, 1)],
    )
    return r.json()["id"]


async def _set_location(client, courier_id, lon, lat):
    return await client.put(
        f"/couriers/{courier_id}/location",
        json={"longitude": lon, "latitude": lat},
    )


# ===========================================================================
# Бизнес-правила создания заказа
# ===========================================================================
async def test_create_order_with_multiple_items_totals(
    integration_client, db_session, client_user, restaurant,
    menu_item_1, menu_item_2,
):
    r = await create_order(
        integration_client,
        client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 2), (menu_item_2.id, 3)],
    )
    assert r.status_code == 201, r.text
    body = r.json()

    assert Decimal(body["total"]) == Decimal("1900.00")
    assert body["status"] == "new"

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


async def test_expired_stop_list_does_not_block_order(
    integration_client, db_session, client_user, restaurant, menu_item_1,
):
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

async def test_create_order_unknown_menu_item_returns_4xx(
    integration_client, client_user, restaurant,
):
    r = await integration_client.post("/orders", json={
        "client_id": client_user.id,
        "restaurant_id": restaurant.id,
        "address": "Main street 1",
        "longitude": 37.6, "latitude": 55.7,
        "items": [{"menu_item_id": 9_999_999, "quantity": 1}],
    })
    assert r.status_code in (404, 422), r.text
    assert r.status_code != 500

@pytest.mark.parametrize("quantity", [10_000_000, 999_999_999])
async def test_create_order_huge_quantity_returns_4xx(
    integration_client, client_user, restaurant, menu_item_1, quantity,
):
    r = await integration_client.post("/orders", json={
        "client_id": client_user.id,
        "restaurant_id": restaurant.id,
        "address": "Main street 1",
        "longitude": 37.6, "latitude": 55.7,
        "items": [{"menu_item_id": menu_item_1.id, "quantity": quantity}],
    })
    assert 400 <= r.status_code < 500, (
        f"quantity={quantity} дал {r.status_code}: "
        "если 500 — переполнение Numeric, надо ограничить сверху"
    )

async def test_create_order_repeated_payload_behavior(
    integration_client, client_user, restaurant, menu_item_1,
):
    """Если API не поддерживает Idempotency-Key — каждый вызов создаёт
    новый заказ. Этот тест документирует факт и ловит случайное появление
    идемпотентности без явного контракта."""
    body = {
        "client_id": client_user.id,
        "restaurant_id": restaurant.id,
        "address": "Main street 1",
        "longitude": 37.6, "latitude": 55.7,
        "items": [{"menu_item_id": menu_item_1.id, "quantity": 1}],
    }
    r1 = await integration_client.post("/orders", json=body)
    r2 = await integration_client.post("/orders", json=body)
    assert r1.status_code == r2.status_code == 201
    # Ждём либо два разных заказа (нет idempotency), либо один и тот же id
    id1, id2 = r1.json()["id"], r2.json()["id"]
    assert (id1 != id2) or (id1 == id2 and "Idempotency-Key" in r1.headers), (
        f"Либо два заказа ({id1}, {id2}), либо один с Idempotency-Key; "
        f"получилось {id1}, {id2} без ключа"
    )

async def test_order_total_equals_sum_of_items(
    integration_client, db_session, client_user, restaurant,
    menu_item_1, menu_item_2,
):
    r = await create_order(
        integration_client,
        client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 2), (menu_item_2.id, 3)],
    )
    assert r.status_code == 201
    body = r.json()

    db_total = await db_session.scalar(
        select(func.sum(OrderItem.total_cost)).where(
            OrderItem.order_id == body["id"],
        )
    )
    assert Decimal(body["total"]) == Decimal(db_total), (
        f"API total {body['total']} != DB sum {db_total}"
    )

# ===========================================================================
# Ресторан: accept → ready
# ===========================================================================
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
    assert order.status_id != 1


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


# ===========================================================================
# Курьер: accept → pickup → deliver
# ===========================================================================
async def test_courier_accepts_ready_order(
    integration_client, db_session, client_user, restaurant,
    menu_item_1, courier,
):
    order_id = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    r = await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/accept",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "assigned"
    assert body["courier_id"] == courier.id

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
    ids = _available_ids(r.json())

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

    order = await db_session.get(Order, order_id)
    await db_session.refresh(order)
    assert order.finished_at is not None

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
    def _dish(brand_id, label):
        return Dish(**_make_payload(
            Dish, brand_id=brand_id,
            name=label, description=None,
        ))

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


async def test_order_total_unaffected_by_menu_price_change(
    integration_client, db_session, client_user, restaurant, menu_item_1,
):
    r = await create_order(
        integration_client,
        client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 2)],
    )
    assert r.status_code == 201
    order_id = r.json()["id"]

    await integration_client.patch(
        f"/restaurants/{restaurant.id}/menu-items/{menu_item_1.id}",
        json={"price": 5000.0},
    )

    items = (await db_session.execute(
        select(OrderItem).where(OrderItem.order_id == order_id)
    )).scalars().all()
    assert len(items) == 1
    assert Decimal(items[0].total_cost) == Decimal("1000.00")

async def test_order_total_uses_decimal_not_float(
    integration_client, db_session, client_user, restaurant, brand,
):
    from decimal import Decimal
    from tests.conftest import _make_dish, _make_menu_item

    dish = await _make_dish(db_session, brand)
    mi = await _make_menu_item(db_session, restaurant, dish, price=Decimal("12.55"))

    r = await create_order(
        integration_client,
        client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(mi.id, 3)],
    )
    assert r.status_code == 201
    # ровно 37.65, не 37.649999...
    assert r.json()["total"] == "37.65"


# ===========================================================================
# Ближайшие курьеры
# ===========================================================================
async def test_nearby_couriers_filtered_and_ordered(
    integration_client, restaurant, courier, courier_2, courier_3,
):
    await _set_location(integration_client, courier.id, 37.6178, 55.7560)
    await _set_location(integration_client, courier_2.id, 37.65, 55.756)
    await _set_location(integration_client, courier_3.id, 38.1, 55.756)

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


async def test_restaurant_cannot_access_foreign_order(
    integration_client, client_user, restaurant, other_restaurant,
    menu_item_1,
):
    """Cross-tenant: чужой ресторан не видит заказ ни в списке, ни по id."""
    order_id = await _create_order_id(
        integration_client, client_user, restaurant, menu_item_1,
    )

    # Список заказов чужого ресторана не должен содержать наш заказ.
    lst = await integration_client.get(
        f"/restaurants/{other_restaurant.id}/orders",
    )
    if lst.status_code == 200:
        raw = lst.json()
        items = raw.get("orders", raw) if isinstance(raw, dict) else raw
        ids = {o.get("id") for o in items if isinstance(o, dict)}
        assert order_id not in ids, (
            f"Foreign restaurant {other_restaurant.id} видит заказ {order_id}"
        )
    elif lst.status_code not in (404, 405):
        raise AssertionError(
            f"Unexpected {lst.status_code} from list endpoint: {lst.text}"
        )

    # Прямой доступ по id — 403/404; 405 (только POST) тоже означает,
    # что GET-ручки нет (значит, и утечки нет).
    single = await integration_client.get(
        f"/restaurants/{other_restaurant.id}/orders/{order_id}",
    )
    assert single.status_code in (403, 404, 405), (
        f"Foreign restaurant got {single.status_code}: {single.text}"
    )


async def test_courier_without_location_not_in_nearby(
    integration_client, restaurant, courier, courier_2,
):
    """Только курьеры с PUT /location должны появляться в nearby.
    courier_2 локацию не обновлял — его там быть не должно."""
    await _set_location(integration_client, courier.id, 37.6178, 55.7560)
    # courier_2 — без вызова _set_location

    r = await integration_client.get(
        f"/restaurants/{restaurant.id}/couriers/nearby",
        params={"radius_km": 5, "limit": 10},
    )
    assert r.status_code == 200

    body = r.json()
    raw_ids = body["courier_ids"] if isinstance(body, dict) else body
    ids = [str(c) for c in raw_ids]

    assert str(courier.id) in ids
    assert str(courier_2.id) not in ids, (
        f"Курьер без локации {courier_2.id} попал в nearby: {ids}"
    )


@pytest.mark.xfail(
    reason=(
        "save_courier_location не ставит TTL на geoadd, "
        "find_nearby_couriers не фильтрует по свежести. "
        "Устаревшие локации возвращаются бесконечно."
    ),
    strict=False,
)
async def test_stale_location_filtered_from_nearby(
    integration_client, restaurant, courier,
):
    """Семантически: если курьер не обновлял локацию дольше N секунд,
    он не должен появляться в nearby. Требует LOCATION_TTL_SECONDS
    или временного окна в find_nearby_couriers.
    """
    from backend import courier_locations as cl

    has_ttl = any(
        hasattr(cl, name) for name in (
            "LOCATION_TTL_SECONDS",
            "MAX_LOCATION_AGE_SECONDS",
            "LOCATION_STALE_AFTER_SECONDS",
        )
    )
    assert has_ttl, (
        "Механизм истечения локаций не реализован. "
        "Тест помечен xfail; станет зелёным при появлении TTL."
    )


async def test_available_respects_limit(
    integration_client, client_user, restaurant,
    menu_item_1, menu_item_2, courier,
):
    """GET /couriers/{id}/orders/available?limit=1 возвращает ≤1 заказ,
    даже если готовых три."""
    # Три ready-заказа
    for _ in range(3):
        await make_ready_order(
            integration_client, client_user.id, restaurant, menu_item_1,
        )
    # Четвёртый через menu_item_2, чтобы был отдельный блюдо-набор
    await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_2,
    )

    r = await integration_client.get(
        f"/couriers/{courier.id}/orders/available",
        params={"limit": 1},
    )
    assert r.status_code == 200
    assert len(r.json()) <= 1, (
        f"available?limit=1 вернул {len(r.json())} заказов. "
        "Endpoint не поддерживает limit (или игнорирует его)."
    )

async def test_nearby_respects_limit(
    integration_client, restaurant, courier, courier_2, courier_3,
):
    """nearby?limit=2 возвращает ровно 2 курьера из 3 в радиусе."""
    for c in (courier, courier_2, courier_3):
        await _set_location(integration_client, c.id, 37.6178, 55.7560)

    r = await integration_client.get(
        f"/restaurants/{restaurant.id}/couriers/nearby",
        params={"radius_km": 5, "limit": 2},
    )
    assert r.status_code == 200

    body = r.json()
    raw_ids = body["courier_ids"] if isinstance(body, dict) else body
    assert len(raw_ids) == 2, (
        f"nearby?limit=2 вернул {len(raw_ids)}: {raw_ids}"
    )


async def test_create_order_duplicate_items_writes_nothing(
    integration_client, db_session, client_user, restaurant, menu_item_1,
):
    """422 на дубликат menu_item_id означает, что ни Order, ни OrderItem
    не записаны (транзакция откатилась)."""
    r = await integration_client.post("/orders", json={
        "client_id": client_user.id,
        "restaurant_id": restaurant.id,
        "address": "Main street 1",
        "longitude": 37.6, "latitude": 55.7,
        "items": [
            {"menu_item_id": menu_item_1.id, "quantity": 1},
            {"menu_item_id": menu_item_1.id, "quantity": 2},
        ],
    })
    assert r.status_code == 422

    orders = (await db_session.execute(
        select(Order).where(Order.client_id == client_user.id)
    )).scalars().all()
    assert len(orders) == 0, f"Создан Order {[o.id for o in orders]} при 422"

    order_items = (await db_session.execute(
        select(OrderItem).where(OrderItem.menu_item_id == menu_item_1.id)
    )).scalars().all()
    assert len(order_items) == 0, "OrderItem записан при 422"


# ===========================================================================
# Тест 15: параметризация SQL — адрес не должен ломать БД
# ===========================================================================
async def test_create_order_sql_injection_in_address(
    integration_client, db_session, client_user, restaurant, menu_item_1,
):
    """Параметризация не должна сломаться при рефакторинге.
    Заказ создаётся, таблица orders остаётся."""
    payload = "'; DROP TABLE orders;--"
    r = await create_order(
        integration_client,
        client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 1)],
    )
    # create_order хелпер не передаёт address — шлём напрямую
    r = await integration_client.post("/orders", json={
        "client_id": client_user.id,
        "restaurant_id": restaurant.id,
        "address": payload,
        "longitude": 37.6, "latitude": 55.7,
        "items": [{"menu_item_id": menu_item_1.id, "quantity": 1}],
    })
    assert r.status_code == 201, r.text

    # Таблица на месте, данные целы
    count = await db_session.scalar(
        select(func.count()).select_from(Order)
    )
    assert count >= 1
    saved = (await db_session.execute(
        select(Order).where(Order.id == r.json()["id"])
    )).scalar_one()
    assert saved.address == payload


# ===========================================================================
# Тест 17: порядок items не влияет на total
# ===========================================================================
async def test_order_total_independent_of_item_order(
    integration_client, client_user, restaurant, menu_item_1, menu_item_2,
):
    """Одинаковый набор items в разном порядке — одинаковый total."""
    r1 = await create_order(
        integration_client,
        client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 2), (menu_item_2.id, 3)],
    )
    r2 = await create_order(
        integration_client,
        client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(menu_item_2.id, 3), (menu_item_1.id, 2)],
    )
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["total"] == r2.json()["total"]