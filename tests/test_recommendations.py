"""Тесты эндпоинта GET /recommendations/{user_id}.

Использует общие фикстуры из conftest:
- integration_client  — HTTP-клиент + реальная тестовая БД + fakeredis
- db_session          — сессия с откатом через savepoint
- client_user, auth_provider, restaurant, menu_item_1, menu_item_2
- create_order        — non-fixture helper: POST /orders

Qdrant и модель эмбеддингов — внешние сервисы, мокаем по месту вызова
в backend.api.recommendations.
"""

import uuid
from unittest.mock import patch

import numpy as np
import pytest

# non-fixture helpers из conftest
from tests.conftest import _make_payload, _persist, create_order
from backend.database.models import Client
from backend.database.session import get_db_session
from backend.recommendations.embeddings import text_to_vector
from backend.recommendations.profiler import get_taste_text
from backend.recommendations.qdrant import VectorItem, SearchQuery, search_similar


# ---------------------------------------------------------------------------
# Локальные фикстуры
# ---------------------------------------------------------------------------

@pytest.fixture
async def other_client(db_session, auth_provider):
    """Второй клиент — «похожий пользователь» в рекомендациях."""
    return await _persist(
        db_session,
        Client(**_make_payload(
            Client,
            auth_provider_id=auth_provider.id,
            external_id=f"other-{uuid.uuid4().hex[:6]}",
        )),
    )


# ---------------------------------------------------------------------------
# 404 — несуществующий пользователь
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_recommendations_user_not_found(integration_client):
    """get_taste_text → None → 404. Мокать ничего не нужно: до эмбеддингов
    и Qdrant управление не доходит."""
    r = await integration_client.get("/recommendations/999999")
    assert r.status_code == 404
    assert r.json()["detail"] == "User not found"


# ---------------------------------------------------------------------------
# Пустой Qdrant — нет похожих пользователей
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_recommendations_no_similar_users(integration_client, client_user):
    with (
        patch(
            "backend.api.recommendations.text_to_vector",
            return_value=np.zeros(4),
        ),
        patch(
            "backend.api.recommendations.search_similar",
            return_value=[],
        ),
    ):
        r = await integration_client.get(f"/recommendations/{client_user.id}")

    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == client_user.id
    assert body["similar_users"] == []
    assert body["dishes"] == []


# ---------------------------------------------------------------------------
# Пользователь сам себе не рекомендуется (self-filter)
# ---------------------------------------------------------------------------

@pytest.mark.contract
async def test_recommendations_excludes_self_from_similar_users(
    integration_client, client_user, other_client,
):
    """Если Qdrant вернул самого пользователя — он должен быть отфильтрован."""
    fake_hits = [
        VectorItem(id=client_user.id, score=0.99),
        VectorItem(id=other_client.id, score=0.42),
    ]
    with (
        patch(
            "backend.api.recommendations.text_to_vector",
            return_value=np.zeros(4),
        ),
        patch(
            "backend.api.recommendations.search_similar",
            return_value=fake_hits,
        ),
    ):
        r = await integration_client.get(f"/recommendations/{client_user.id}")

    assert r.status_code == 200
    similar_ids = [u["user_id"] for u in r.json()["similar_users"]]
    assert similar_ids == [other_client.id]


# ---------------------------------------------------------------------------
# Полный сценарий: рекомендуем то, что заказывали похожие, но не сам
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_recommendations_exclude_own_dishes(
    integration_client,
    db_session,
    client_user,
    other_client,
    restaurant,
    menu_item_1,
    menu_item_2,
):
    # A (target) заказывает dish1
    r_a = await create_order(
        integration_client,
        client_id=client_user.id,
        restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 1)],
    )
    assert r_a.status_code == 201, r_a.text

    # B (похожий) заказывает dish1 и dish2
    r_b = await create_order(
        integration_client,
        client_id=other_client.id,
        restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 1), (menu_item_2.id, 1)],
    )
    assert r_b.status_code == 201, r_b.text

    # Qdrant говорит, что B похож на A
    fake_hits = [VectorItem(id=other_client.id, score=0.9)]

    with (
        patch(
            "backend.api.recommendations.text_to_vector",
            return_value=np.zeros(4),
        ),
        patch(
            "backend.api.recommendations.search_similar",
            return_value=fake_hits,
        ),
    ):
        r = await integration_client.get(
            f"/recommendations/{client_user.id}",
            params={"similar_limit": 5, "dishes_limit": 10},
        )

    assert r.status_code == 200
    body = r.json()

    recommended_dish_ids = {d["dish_id"] for d in body["dishes"]}

    # dish2 B заказывал, A — нет → должна попасть в рекомендации
    assert menu_item_2.dish_id in recommended_dish_ids
    # dish1 A уже заказывал → исключаем
    assert menu_item_1.dish_id not in recommended_dish_ids


# ---------------------------------------------------------------------------
# Ограничение dishes_limit соблюдается
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_recommendations_respects_dishes_limit(
    integration_client,
    client_user,
    other_client,
    restaurant,
    menu_item_1,
    menu_item_2,
):
    # B заказывает оба блюда, A — ничего, чтобы оба попали в рекомендации
    r_b = await create_order(
        integration_client,
        client_id=other_client.id,
        restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 1), (menu_item_2.id, 1)],
    )
    assert r_b.status_code == 201, r_b.text

    fake_hits = [VectorItem(id=other_client.id, score=0.9)]

    with (
        patch(
            "backend.api.recommendations.text_to_vector",
            return_value=np.zeros(4),
        ),
        patch(
            "backend.api.recommendations.search_similar",
            return_value=fake_hits,
        ),
    ):
        r = await integration_client.get(
            f"/recommendations/{client_user.id}",
            params={"dishes_limit": 1},
        )

    assert r.status_code == 200
    assert len(r.json()["dishes"]) <= 1