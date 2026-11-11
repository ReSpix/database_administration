from unittest.mock import AsyncMock, Mock

import pytest

from backend.database.models import Client
from backend.recomendations.profiler import get_taste_text, text_template

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
