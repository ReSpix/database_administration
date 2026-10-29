"""State machine заказа: запрет перескоков, повторов и движения назад.

Ожидаемые коды нарушения — 409 Conflict (соглашение из test_contract.py).
Если API фактически возвращает 404 на accept не-ready заказа
(«такого нет в пуле»), поправьте соответствующую строку на `in (409, 404)`.

Сквозные happy-path переходы (accept→ready, accept→pickup→deliver) —
в test_integration.py. Здесь только негативные переходы.
"""
import pytest

from tests.conftest import create_order, make_ready_order

pytestmark = pytest.mark.integration


# ===========================================================================
# Ресторан: accept / ready
# ===========================================================================
async def test_restaurant_cannot_mark_ready_before_accepted(
    integration_client, client_user, restaurant, menu_item_1,
):
    """new → ready (минуя accepted) запрещён."""
    r = await create_order(
        integration_client,
        client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 1)],
    )
    order_id = r.json()["id"]

    resp = await integration_client.post(
        f"/restaurants/{restaurant.id}/orders/{order_id}/ready",
        json={},
    )
    assert resp.status_code == 409, resp.text


async def test_restaurant_cannot_mark_ready_twice(
    integration_client, client_user, restaurant, menu_item_1,
):
    """ready → ready запрещён."""
    order_id = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    resp = await integration_client.post(
        f"/restaurants/{restaurant.id}/orders/{order_id}/ready",
        json={},
    )
    assert resp.status_code == 409, resp.text


async def test_restaurant_cannot_accept_ready_order(
    integration_client, client_user, restaurant, menu_item_1,
):
    """ready → accepted (назад по состояниям) запрещён."""
    order_id = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    resp = await integration_client.post(
        f"/restaurants/{restaurant.id}/orders/{order_id}/accept",
    )
    assert resp.status_code == 409, resp.text


# ===========================================================================
# Курьер: accept
# ===========================================================================
async def test_courier_cannot_accept_new_order(
    integration_client, client_user, restaurant, menu_item_1, courier,
):
    """new → assigned (минуя accepted+ready) запрещён."""
    r = await create_order(
        integration_client,
        client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 1)],
    )
    order_id = r.json()["id"]

    resp = await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/accept",
    )
    assert resp.status_code in (409, 404), resp.text


async def test_courier_cannot_accept_accepted_order(
    integration_client, client_user, restaurant, menu_item_1, courier,
):
    """accepted → assigned запрещён: нужно ready."""
    r = await create_order(
        integration_client,
        client_id=client_user.id, restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 1)],
    )
    order_id = r.json()["id"]
    await integration_client.post(
        f"/restaurants/{restaurant.id}/orders/{order_id}/accept",
    )

    resp = await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/accept",
    )
    assert resp.status_code in (409, 404), resp.text


# ===========================================================================
# Курьер: pickup
# ===========================================================================
async def test_courier_cannot_pickup_without_accept(
    integration_client, client_user, restaurant, menu_item_1, courier,
):
    """ready → picked_up (минуя assigned) запрещён."""
    order_id = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    resp = await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/pickup",
    )
    assert resp.status_code == 409, resp.text


async def test_courier_cannot_pickup_twice(
    integration_client, client_user, restaurant, menu_item_1, courier,
):
    """picked_up → picked_up запрещён."""
    order_id = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    assert (await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/accept",
    )).status_code == 200
    assert (await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/pickup",
    )).status_code == 200

    resp = await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/pickup",
    )
    assert resp.status_code == 409, resp.text


# ===========================================================================
# Курьер: deliver
# ===========================================================================
async def test_courier_cannot_deliver_before_pickup(
    integration_client, client_user, restaurant, menu_item_1, courier,
):
    """assigned → delivered (минуя picked_up) запрещён."""
    order_id = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    assert (await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/accept",
    )).status_code == 200

    resp = await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/deliver",
    )
    assert resp.status_code == 409, resp.text


async def test_courier_cannot_deliver_twice(
    integration_client, client_user, restaurant, menu_item_1, courier,
):
    """delivered → delivered запрещён."""
    order_id = await make_ready_order(
        integration_client, client_user.id, restaurant, menu_item_1,
    )
    await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/accept",
    )
    await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/pickup",
    )
    assert (await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/deliver",
    )).status_code == 200

    resp = await integration_client.post(
        f"/couriers/{courier.id}/orders/{order_id}/deliver",
    )
    assert resp.status_code == 409, resp.text