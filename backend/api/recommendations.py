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
    score: int          # суммарное amount по заказам похожих пользователей


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
    return [
        SimilarUser(user_id=hit.id, score=float(hit.score or 0.0))
        for hit in hits
        if hit.id != user_id
    ][:limit]


async def _recommend_dishes(
    session: AsyncSession,
    user_id: int,
    similar_user_ids: list[int],
    limit: int,
) -> list[DishRecommendation]:
    if not similar_user_ids:
        return []

    # блюда, которые уже заказывал сам пользователь — их исключаем
    ordered_by_user = (
        select(MenuItem.dish_id)
        .join(OrderItem, OrderItem.menu_item_id == MenuItem.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.client_id == user_id)
    )

    total = func.sum(OrderItem.amount).label("total")
    stmt = (
        select(Dish.id, Dish.name, total)
        .select_from(Order)
        .join(Order.items)
        .join(OrderItem.menu_item)
        .join(MenuItem.dish)
        .where(Order.client_id.in_(similar_user_ids))
        .where(~Dish.id.in_(ordered_by_user))
        .group_by(Dish.id, Dish.name)
        .order_by(total.desc(), Dish.name)
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