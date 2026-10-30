"""Множественные сущности: несколько заказов, клиентов, ресторанов.

Проверяет, что:
- одна позиция меню в разных заказах не «склеивается» в OrderItem
- несколько клиентов одного ресторана обслуживаются независимо
- два готовых заказа одновременно видны курьеру (нет LIMIT=1)
- курьер видит заказы из разных ресторанов
- два курьера принимают разные заказы без пересечений
- batching: несколько заказов одному курьеру
- уникальность Client по (provider, external_id)

Один заказ — два курьера → test_concurrency.py.
"""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.database.models import Client, OrderItem
from tests.conftest import _make_payload, _persist, create_order, make_ready_order, _available_ids

pytestmark = pytest.mark.integration


# ===========================================================================
# Одна позиция меню — разные заказы
# ===========================================================================
async def test_same_menu_item_in_multiple_orders_keeps_separate_totals(
    integration_client, db_session, client_user, restaurant, menu_item_1,
):
    """Один menu_item в двух заказах: у каждого свой OrderItem,
    total_cost не смешивается."""
    r1 = await create_order(
        integration_client, client_id=client_user.id,
        restaurant_id=restaurant.id, items=[(menu_item_1.id, 2)],
    )
    r2 = await create_order(
        integration_client, client_id=client_user.id,
        restaurant_id=restaurant.id, items=[(menu_item_1.id, 5)],
    )
    assert r1.status_code == 201 and r2.status_code == 201
    id1, id2 = r1.json()["id"], r2.json()["id"]
    assert id1 != id2

    rows = (await db_session.execute(
        select(OrderItem).where(OrderItem.order_id.in_([id1, id2]))
    )).scalars().all()
    by_order: dict[int, list] = {}
    for it in rows:
        by_order.setdefault(it.order_id, []).append(it)

    assert len(by_order[id1]) == 1
    assert by_order[id1][0].amount == 2
    assert Decimal(by_order[id1][0].total_cost) == Decimal("1000.00")

    assert len(by_order[id2]) == 1
    assert by_order[id2][0].amount == 5
    assert Decimal(by_order[id2][0].total_cost) == Decimal("2500.00")


# ===========================================================================
# Несколько клиентов — один ресторан
# ===========================================================================
async def test_restaurant_accepts_orders_from_multiple_clients(
    integration_client, db_session, client_user, auth_provider,
    restaurant, menu_item_1,
):
    """Два клиента заказывают из одного ресторана — оба заказа
    принимаются независимо."""
    tag = uuid.uuid4().hex[:6]
    client2 = await _persist(db_session, Client(**_make_payload(
        Client,
        auth_provider_id=auth_provider.id,
        external_id=f"client2-{tag}",
    )))
    assert client2.id != client_user.id

    r1 = await create_order(
        integration_client, client_id=client_user.id,
        restaurant_id=restaurant.id, items=[(menu_item_1.id, 1)],
    )
    r2 = await create_order(
        integration_client, client_id=client2.id,
        restaurant_id=restaurant.id, items=[(menu_item_1.id, 1)],
    )
    assert r1.status_code == 201 and r2.status_code == 201

    ok1 = await integration_client.post(
        f"/restaurants/{restaurant.id}/orders/{r1.json()['id']}/accept",
    )
    ok2 = await integration_client.post(
        f"/restaurants/{restaurant.id}/orders/{r2.json()['id']}/accept",
    )
    assert ok1.status_code == 200
    assert ok2.status_code == 200


# ===========================================================================
# Несколько готовых — один курьер
# ===========================================================================
async def test_courier_sees_all_ready_orders(
    integration_client, client_user, restaurant,
    menu_item_1, menu_item_2, courier,
):
    """Два ready-заказа одновременно — оба в available.
    Ловит неявный LIMIT=1 или «брать только первый»."""
    def _ids(body):
        return {
            (o.get("id", o.get("order_id")) if isinstance(o, dict) else o)
            for o in body
        }

    order_1 = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    order_2 = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_2,
    )
    assert order_1 != order_2

    r = await integration_client.get(
        f"/couriers/{courier.id}/orders/available",
    )
    assert r.status_code == 200
    seen = _ids(r.json())

    assert order_1 in seen, f"{order_1} не в available: {seen}"
    assert order_2 in seen, f"{order_2} не в available: {seen}"


# ===========================================================================
# Курьер — несколько ресторанов
# ===========================================================================
async def test_courier_sees_ready_orders_from_multiple_restaurants(
    integration_client, client_user, restaurant, other_restaurant,
    menu_item_1, menu_item_other_restaurant, courier,
):
    """available не ограничен одним рестораном (когда курьер свободен)."""
    def _ids(body):
        return {
            (o.get("id", o.get("order_id")) if isinstance(o, dict) else o)
            for o in body
        }

    order_1 = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    order_2 = await make_ready_order(
        integration_client, client_user.id, other_restaurant,
        menu_item_other_restaurant,
    )

    r = await integration_client.get(
        f"/couriers/{courier.id}/orders/available",
    )
    assert r.status_code == 200
    seen = _ids(r.json())

    assert order_1 in seen
    assert order_2 in seen


# ===========================================================================
# Два курьера — два разных заказа
# ===========================================================================
async def test_two_couriers_accept_different_orders_independently(
    integration_client, client_user, restaurant,
    menu_item_1, menu_item_2, courier, courier_2,
):
    """Первый заказ — на courier, второй — на courier_2.
    Оба назначения успешны и указывают на своих курьеров."""
    order_1 = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    order_2 = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_2,
    )

    a = await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_1}/accept",
    )
    b = await integration_client.post(
        f"/couriers/{courier_2.id}/orders/{order_2}/accept",
    )
    assert a.status_code == 200, a.text
    assert b.status_code == 200, b.text
    assert a.json()["courier_id"] == courier.id
    assert b.json()["courier_id"] == courier_2.id

# ===========================================================================
# Batching: несколько заказов одному курьеру
# ===========================================================================
async def test_courier_with_assigned_order_can_take_batch_from_same_restaurant(
    integration_client, client_user, restaurant,
    menu_item_1, menu_item_2, courier,
):
    """Курьер уже accepted заказ из X. Новый ready из X должен быть виден
    и принимаем (политика batching по ресторану)."""
    order_1 = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    assert (await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_1}/accept",
    )).status_code == 200

    order_2 = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_2,
    )

    avail = await integration_client.get(
        f"/couriers/{courier.id}/orders/available",
    )
    assert avail.status_code == 200
    seen = _available_ids(avail.json())
    assert order_2 in seen, (
        f"Batching разрешён, но {order_2} не в available: {seen}"
    )

    r2 = await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_2}/accept",
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "assigned"


# async def test_courier_with_assigned_order_does_not_get_order_from_other_restaurant(
#     integration_client, client_user, restaurant, other_restaurant,
#     menu_item_1, menu_item_other_restaurant, courier,
# ):
#     """Курьер едет в X. Заказ из Y не по пути — не виден и не принимается."""
#     order_1 = await make_ready_order(
#         integration_client, client_user.id, restaurant, menu_item_1,
#     )
#     assert (await integration_client.post(
#         f"/couriers/{courier.id}/orders/{order_1}/accept",
#     )).status_code == 200

#     order_other = await make_ready_order(
#         integration_client, client_user.id, other_restaurant,
#         menu_item_other_restaurant,
#     )

#     avail = await integration_client.get(
#         f"/couriers/{courier.id}/orders/available",
#     )
#     seen = _available_ids(avail.json())
#     assert order_other not in seen

#     r = await integration_client.post(
#         f"/couriers/{courier.id}/orders/{order_other}/accept",
#     )
#     assert r.status_code in (409, 404)


# async def test_courier_after_pickup_does_not_see_batch(
#     integration_client, client_user, restaurant,
#     menu_item_1, menu_item_2, courier,
# ):
#     """После pickup курьер едет к клиенту. Возвращаться в ресторан
#     за новым заказом не должен."""
#     order_1 = await make_ready_order(
#         integration_client, client_user.id, restaurant, menu_item_1,
#     )
#     assert (await integration_client.post(
#         f"/couriers/{courier.id}/orders/{order_1}/accept",
#     )).status_code == 200
#     assert (await integration_client.post(
#         f"/couriers/{courier.id}/orders/{order_1}/pickup",
#     )).status_code == 200

#     order_2 = await make_ready_order(
#         integration_client, client_user.id, restaurant, menu_item_2,
#     )
#     avail = await integration_client.get(
#         f"/couriers/{courier.id}/orders/available",
#     )
#     seen = _available_ids(avail.json())
#     assert order_2 not in seen


# ===========================================================================
# Client: уникальность (auth_provider, external_id)
# ===========================================================================
async def test_duplicate_external_id_per_provider_rejected(
    db_session, auth_provider,
):
    from sqlalchemy.exc import IntegrityError

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

async def test_same_external_id_across_providers_allowed(
    db_session,
):
    from backend.database.models import AuthProvider, Client
    from tests.conftest import _make_payload, _persist

    p1 = await _persist(db_session, AuthProvider(**_make_payload(
        AuthProvider, name="p1",
    )))
    p2 = await _persist(db_session, AuthProvider(**_make_payload(
        AuthProvider, name="p2",
    )))

    c1 = await _persist(db_session, Client(**_make_payload(
        Client, auth_provider_id=p1.id, external_id="same-user",
    )))
    c2 = await _persist(db_session, Client(**_make_payload(
        Client, auth_provider_id=p2.id, external_id="same-user",
    )))
    assert c1.id != c2.id