from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import Dish, MenuItem, Order, OrderItem
from backend.database.session import get_db_session
from backend.recommendations.embeddings import text_to_vector
from backend.recommendations.profiler import get_taste_text
from backend.recommendations.qdrant import SearchQuery, search_similar

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

MAX_LIMIT = 20
DEFAULT_SIMILAR_LIMIT = 5
DEFAULT_DISHES_LIMIT = 10


class SimilarUser(BaseModel):
    user_id: int
    score: float


class DishRecommendation(BaseModel):
    dish_id: int
    name: str
    score: int  # суммарное amount по заказам похожих пользователей


class RecommendationResponse(BaseModel):
    user_id: int
    similar_users: list[SimilarUser]
    dishes: list[DishRecommendation]


async def _find_similar_users(
    user_id: int, profile_text: str, limit: int
) -> list[SimilarUser]:
    vector = text_to_vector(profile_text).tolist()
    hits = search_similar(SearchQuery(vector=vector, limit=limit + 1))
    # +1 чтобы был шанс отфильтровать самого себя, если он сохранён в Qdrant
    result = [
        SimilarUser(user_id=hit.id, score=float(hit.score or 0.0))
        for hit in hits
        if hit.id != user_id
    ][:limit]
    for i in result:
        print(i)
    return result


from sqlalchemy import exists, func, select


async def _recommend_dishes(
    session: AsyncSession,
    user_id: int,
    similar_user_ids: list[int],
    limit: int,
) -> list[DishRecommendation]:
    if not similar_user_ids:
        return []

    # Проверка через NOT EXISTS работает быстрее и безопаснее, чем NOT IN
    already_ordered = exists(
        select(1)
        .select_from(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(MenuItem, MenuItem.id == OrderItem.menu_item_id)
        .where(
            Order.client_id == user_id,
            MenuItem.dish_id == Dish.id,
        )
    )

    total = func.sum(OrderItem.amount).label("total")

    # Сначала считаем суммы только по ID (без лишнего JOIN и группировки по тексту)
    top_dishes_subq = (
        select(MenuItem.dish_id.label("dish_id"), total)
        .select_from(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(MenuItem, MenuItem.id == OrderItem.menu_item_id)
        .where(Order.client_id.in_(similar_user_ids))
        .group_by(MenuItem.dish_id)
        .subquery()
    )

    # Присоединяем названия и отсекаем уже заказанные блюда
    stmt = (
        select(Dish.id, Dish.name, top_dishes_subq.c.total)
        .join(top_dishes_subq, top_dishes_subq.c.dish_id == Dish.id)
        .where(~already_ordered)
        .order_by(top_dishes_subq.c.total.desc(), Dish.name)
        .limit(limit)
    )

    rows = (await session.execute(stmt)).all()
    return [
        DishRecommendation(dish_id=dish_id, name=name, score=int(amount))
        for dish_id, name, amount in rows
    ]


@router.get("/{user_id}", response_model=RecommendationResponse)
async def get_recommendations(
    user_id: int,
    similar_limit: int = Query(DEFAULT_SIMILAR_LIMIT, ge=1, le=MAX_LIMIT),
    dishes_limit: int = Query(DEFAULT_DISHES_LIMIT, ge=1, le=MAX_LIMIT),
    session: AsyncSession = Depends(get_db_session),
) -> RecommendationResponse:
    profile_text = await get_taste_text(session, user_id)
    if profile_text is None:
        raise HTTPException(status_code=404, detail="User not found")

    similar = await _find_similar_users(user_id, profile_text, similar_limit)
    dishes = await _recommend_dishes(
        session, user_id, [s.user_id for s in similar], dishes_limit
    )
    return RecommendationResponse(user_id=user_id, similar_users=similar, dishes=dishes)
