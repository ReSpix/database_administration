"""Тесты эндпоинта GET /recommendations/{user_id}.

Использует общие фикстуры из conftest:
- integration_client  — HTTP-клиент + реальная тестовая БД + fakeredis
- db_session          — сессия с откатом через savepoint
- client_user, auth_provider, restaurant, menu_item_1, menu_item_2
- create_order        — non-fixture helper: POST /orders

Большинство тестов изолируют поиск и эмбеддинги моками. Полный сценарий внизу
использует настоящие эмбеддинги и отдельную временную коллекцию Qdrant.
"""

import uuid
from contextlib import asynccontextmanager
from unittest.mock import patch

import numpy as np
import pytest

# non-fixture helpers из conftest
from tests.conftest import (
    _make_menu_item,
    _make_payload,
    _persist,
    create_order,
)
from backend.database.models import Client, Dish
from backend.recommendations.embeddings import text_to_vector
from backend.recommendations.profiler import text_template
from backend.recommendations.qdrant import VectorItem


# ---------------------------------------------------------------------------
# Локальные фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture
async def other_client(db_session, auth_provider):
    """Второй клиент — «похожий пользователь» в рекомендациях."""
    return await _persist(
        db_session,
        Client(
            **_make_payload(
                Client,
                auth_provider_id=auth_provider.id,
                external_id=f"other-{uuid.uuid4().hex[:6]}",
            )
        ),
    )


@pytest.fixture
def real_qdrant_collection(monkeypatch):
    """Изолированная коллекция на реальном Qdrant для интеграционных тестов."""
    import os

    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    from backend.recommendations import qdrant

    client = QdrantClient(url=os.getenv("TEST_QDRANT_URL", "http://localhost:6333"))
    collection_name = f"test_recommendations_{uuid.uuid4().hex}"
    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(
            size=qdrant.VECTOR_SIZE,
            distance=Distance.COSINE,
        ),
    )
    monkeypatch.setattr(qdrant, "client", client)
    monkeypatch.setattr(qdrant, "COLLECTION_NAME", collection_name)
    try:
        yield client, collection_name
    finally:
        try:
            client.delete_collection(collection_name)
        finally:
            client.close()


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
    integration_client,
    client_user,
    other_client,
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


# ---------------------------------------------------------------------------
# Сценарий с несколькими группами покупателей и историей в реальной БД
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_recommendations_for_pizza_only_user_include_burgers(
    integration_client,
    db_session,
    auth_provider,
    client_user,
    restaurant,
    brand,
    monkeypatch,
    real_qdrant_collection,
):
    """Сохраняет пользователей, генерирует эмбеддинги и ищет похожих в Qdrant.

    Пять пользователей заказывали бургеры и пиццу.
    Еще пять пользователей заказывали суши и роллы.

    Целевой пользователь (одиннадцатый) заказывал только пиццу, поэтому
    бургер должен попасть в рекомендации, а суши и роллы — нет.
    """
    from backend.recommendations import profile_update

    @asynccontextmanager
    async def use_test_db_session():
        yield db_session

    monkeypatch.setattr(profile_update, "async_session_factory", use_test_db_session)

    users = [client_user]
    for _ in range(10):
        users.append(
            await _persist(
                db_session,
                Client(
                    **_make_payload(
                        Client,
                        auth_provider_id=auth_provider.id,
                        external_id=f"recommendation-{uuid.uuid4().hex}",
                    )
                ),
            )
        )

    menu_items = {}
    for name in ("Бургер", "Пицца", "Суши", "Роллы"):
        dish = await _persist(
            db_session,
            Dish(**_make_payload(Dish, brand_id=brand.id, name=name)),
        )
        menu_items[name] = await _make_menu_item(
            db_session, restaurant, dish, price=500
        )

    orders = [
        (users[:5], [menu_items["Бургер"], menu_items["Пицца"]]),
        (users[5:10], [menu_items["Суши"], menu_items["Роллы"]]),
        ([users[10]], [menu_items["Пицца"]]),
    ]
    for cohort, items in orders:
        for user in cohort:
            response = await create_order(
                integration_client,
                client_id=user.id,
                restaurant_id=restaurant.id,
                items=[(item.id, 1) for item in items],
            )
            assert response.status_code == 201, response.text

    qdrant_client, collection_name = real_qdrant_collection
    assert qdrant_client.count(collection_name).count == len(users)

    response = await integration_client.get(f"/recommendations/{users[10].id}")

    assert response.status_code == 200, response.text
    body = response.json()
    recommended_dishes = {dish["name"]: dish["score"] for dish in body["dishes"]}
    assert recommended_dishes == {"Бургер": 5}


@pytest.mark.integration
def test_pizza_embedding_is_closer_to_burger_pizza_than_sushi_rolls():
    profiles = [
        {
            "user_id": 1,
            "kitchens": [],
            "dish_types": [],
            "last_orders": [("Бургер", 1), ("Пицца", 1)],
        },
        {
            "user_id": 2,
            "kitchens": [],
            "dish_types": [],
            "last_orders": [("Пицца", 1)],
        },
        {
            "user_id": 3,
            "kitchens": [],
            "dish_types": [],
            "last_orders": [("Суши", 1), ("Роллы", 1)],
        },
    ]
    burger_pizza, pizza, sushi_rolls = (
        text_to_vector(text_template(profile)) for profile in profiles
    )

    def cosine_distance(first: np.ndarray, second: np.ndarray) -> float:
        cosine_similarity = np.dot(first, second) / (
            np.linalg.norm(first) * np.linalg.norm(second)
        )
        return float(1 - cosine_similarity)

    assert cosine_distance(pizza, burger_pizza) < cosine_distance(
        pizza, sushi_rolls
    )
