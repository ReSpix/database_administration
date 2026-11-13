from unittest.mock import AsyncMock, Mock
from contextlib import asynccontextmanager

import numpy as np
import pytest

from tests.conftest import create_order

from backend.database.models import Client
from backend.recommendations.profiler import get_taste_text, text_template
from backend.recommendations import profile_update
from backend.recommendations.qdrant import VectorItem

pytestmark = pytest.mark.unit


def test_text_template_formats_lists_and_empty_sections():
    text = text_template(
        {
            "user_id": 12,
            "kitchens": [("Итальянская", 4)],
            "dish_types": [],
            "last_orders": [("Пицца", 2)],
        }
    )

    assert "1. Итальянская - 4 шт." in text
    assert "Любимые типы блюд:\nНет данных" in text
    assert "1. Пицца - 2 шт." in text


async def test_get_taste_text_aggregates_tags_and_includes_recent_items():
    session = AsyncMock()
    session.get.return_value = object()
    session.execute.side_effect = [
        Mock(
            all=Mock(
                return_value=[
                    ("kitchen", "Итальянская", 5),
                    ("dish_type", "Пицца", 3),
                    ("unrelated", "Острое", 2),
                ]
            )
        ),
        Mock(all=Mock(return_value=[("Пицца", 2), ("Салат", 1)])),
    ]

    text = await get_taste_text(session, 12)

    session.get.assert_awaited_once_with(Client, 12)
    assert text is not None
    assert session.execute.await_count == 2
    assert "1. Итальянская - 5 шт." in text
    assert "1. Пицца - 3 шт." in text
    assert "1. Пицца - 2 шт." in text
    assert "Острое" not in text


async def test_get_taste_text_returns_none_for_unknown_client():
    session = AsyncMock()
    session.get.return_value = None

    assert await get_taste_text(session, 404) is None
    session.execute.assert_not_awaited()

###
@pytest.mark.contract
async def test_order_creation_enqueues_embedding_refresh(
    integration_client,
    client_user,
    restaurant,
    menu_item_1,
    monkeypatch,
):
    from backend.api import public

    calls: list[int] = []

    async def _spy(user_id: int) -> None:
        calls.append(user_id)

    monkeypatch.setattr(public, "refresh_user_embedding", _spy)

    r = await create_order(
        integration_client,
        client_id=client_user.id,
        restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 1)],
    )

    assert r.status_code == 201, r.text
    assert calls == [client_user.id], (
        "Фоновая задача должна быть поставлена ровно один раз "
        "и с правильным user_id"
    )


# ---------------------------------------------------------------------------
# 2. HTTP-уровень: если фоновая задача падает — заказ всё равно создан
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_order_creation_survives_embedding_refresh_failure(
    integration_client,
    client_user,
    restaurant,
    menu_item_1,
    monkeypatch,
):
    """`refresh_user_embedding` сам ловит исключения, но проверим E2E."""
    from backend.recommendations import profile_update

    def _boom(text: str):
        raise RuntimeError("embedding model down")

    monkeypatch.setattr(profile_update, "text_to_vector", _boom)

    # реальная фоновая задача выполнится (autouse-фикстура не активна:
    # мы патчим public.refresh_user_embedding? нет — мы патчим его
    # внутренности, значит нужно вернуть настоящую функцию)
    from backend.api import public
    monkeypatch.undo()
    monkeypatch.setattr(public, "refresh_user_embedding",
                        profile_update.refresh_user_embedding)
    monkeypatch.setattr(profile_update, "text_to_vector", _boom)

    r = await create_order(
        integration_client,
        client_id=client_user.id,
        restaurant_id=restaurant.id,
        items=[(menu_item_1.id, 1)],
    )

    assert r.status_code == 201, r.text
    assert "id" in r.json()


# ---------------------------------------------------------------------------
# 3. Юнит-уровень: сам refresh_user_embedding — генерирует вектор и апсертит
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_refresh_user_embedding_upserts_vector(
    db_session,
    client_user,
    monkeypatch,
):
    """Патчим async_session_factory (отдаёт тестовую сессию), text_to_vector
    и upsert_point — проверяем, что функция правильно собрала профиль,
    вызвала эмбеддинг и сделала upsert с id=user_id."""

    @asynccontextmanager
    async def _fake_factory():
        yield db_session

    monkeypatch.setattr(profile_update, "async_session_factory", _fake_factory)
    monkeypatch.setattr(
        profile_update, "text_to_vector",
        lambda text: np.ones(4),
    )

    captured: list[VectorItem] = []
    monkeypatch.setattr(
        profile_update, "upsert_point",
        lambda item: captured.append(item),
    )

    await profile_update.refresh_user_embedding(client_user.id)

    assert len(captured) == 1
    item = captured[0]
    assert item.id == client_user.id
    assert item.vector == [1.0, 1.0, 1.0, 1.0]
    assert isinstance(item.payload, dict) and "text" in item.payload


# ---------------------------------------------------------------------------
# 4. Юнит-уровень: user not found → никакого апсерта
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_refresh_user_embedding_skips_unknown_user(
    db_session,
    monkeypatch,
):
    @asynccontextmanager
    async def _fake_factory():
        yield db_session

    monkeypatch.setattr(profile_update, "async_session_factory", _fake_factory)
    monkeypatch.setattr(
        profile_update, "text_to_vector",
        lambda text: pytest.fail("Не должны вызывать эмбеддинг"),
    )
    monkeypatch.setattr(
        profile_update, "upsert_point",
        lambda item: pytest.fail("Не должны делать upsert"),
    )

    await profile_update.refresh_user_embedding(999_999)  # не должен упасть


# ---------------------------------------------------------------------------
# 5. Юнит-уровень: падение в Qdrant не пробрасывается наружу
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_refresh_user_embedding_swallows_errors(
    db_session,
    client_user,
    monkeypatch,
):
    @asynccontextmanager
    async def _fake_factory():
        yield db_session

    monkeypatch.setattr(profile_update, "async_session_factory", _fake_factory)
    monkeypatch.setattr(
        profile_update, "text_to_vector",
        lambda text: np.zeros(4),
    )
    monkeypatch.setattr(
        profile_update, "upsert_point",
        lambda item: (_ for _ in ()).throw(RuntimeError("qdrant down")),
    )

    # Не должно бросить — функция глушит ошибки
    await profile_update.refresh_user_embedding(client_user.id)