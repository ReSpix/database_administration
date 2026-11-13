"""Пересчёт и сохранение эмбеддинга вкусового профиля пользователя."""

import logging

from backend.database.session import async_session_factory
from backend.recommendations.embeddings import text_to_vector
from backend.recommendations.profiler import get_taste_text
from backend.recommendations.qdrant import VectorItem, upsert_point

logger = logging.getLogger(__name__)


async def refresh_user_embedding(user_id: int) -> None:
    """Пересчитать эмбеддинг профиля пользователя и обновить его в Qdrant.

    Вызывается из фоновой задачи после создания заказа.
    Открывает собственную БД-сессию (сессия запроса к этому моменту закрыта).
    Никогда не пробрасывает исключения — они логируются, чтобы не ломать
    основной сценарий и не плодить шум в логах фоновых задач.
    """
    try:
        # 1. Текст профиля — быстро, сессию закрываем до тяжёлых шагов,
        #    чтобы не держать соединение с БД на время эмбеддинга.
        async with async_session_factory() as session:
            profile_text = await get_taste_text(session, user_id)

        if profile_text is None:
            logger.warning("User %s not found, skip embedding refresh", user_id)
            return

        # 2. Эмбеддинг и апсерт — идемпотентно, id = user_id.
        vector = text_to_vector(profile_text).tolist()
        upsert_point(
            VectorItem(
                id=user_id,
                vector=vector,
                payload={"text": profile_text},  # удобно для отладки/витрины
            )
        )
        logger.info("Embedding refreshed for user %s", user_id)
    except Exception:
        logger.exception("Failed to refresh embedding for user %s", user_id)