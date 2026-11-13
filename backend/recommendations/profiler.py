from typing import TypedDict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import (
    Client,
    Dish,
    MenuItem,
    Order,
    OrderItem,
    Tag,
    TagAssignment,
    TagType,
)

RECENT_ORDER_ITEMS_LIMIT = 10
KITCHEN_TAG_TYPES = {"kitchen", "cuisine", "кухня", "кухни"}
DISH_TYPE_TAG_TYPES = {
    "dish_type",
    "dish_types",
    "тип_блюда",
    "типы_блюд",
}


class TasteProfile(TypedDict):
    user_id: int
    kitchens: list[tuple[str, int]]
    dish_types: list[tuple[str, int]]
    last_orders: list[tuple[str, int]]


def _format_items(items: list[tuple[str, int]]) -> str:
    if not items:
        return "Нет данных"
    return "\n".join(
        f"{index}. {name} - {amount} шт."
        for index, (name, amount) in enumerate(items, start=1)
    )


def text_template(data: TasteProfile) -> str:
    return f"""Профиль вкусов пользователя: {data["user_id"]}

Любимые кухни:
{_format_items(data["kitchens"])}

Любимые типы блюд:
{_format_items(data["dish_types"])}

Последние заказы:
{_format_items(data["last_orders"])}
"""


def _normalize_tag_type(name: str) -> str:
    return "_".join(name.casefold().replace("-", " ").split())


async def get_taste_text(session: AsyncSession, user_id: int) -> str | None:
    user = await session.get(Client, user_id)
    if user is None:
        return None

    amount = func.sum(OrderItem.amount).label("amount")
    taste_query = (
        select(TagType.name, Tag.name, amount)
        .select_from(Order)
        .join(Order.items)
        .join(OrderItem.menu_item)
        .join(MenuItem.dish)
        .join(Dish.tag_assignments)
        .join(TagAssignment.tag)
        .join(Tag.type)
        .where(Order.client_id == user_id)
        .group_by(TagType.name, Tag.name)
        .order_by(amount.desc(), Tag.name)
    )
    taste_rows = (await session.execute(taste_query)).all()

    kitchens: list[tuple[str, int]] = []
    dish_types: list[tuple[str, int]] = []
    for type_name, tag_name, total_amount in taste_rows:
        normalized_type = _normalize_tag_type(type_name)
        if normalized_type in KITCHEN_TAG_TYPES:
            kitchens.append((tag_name, total_amount))
        elif normalized_type in DISH_TYPE_TAG_TYPES:
            dish_types.append((tag_name, total_amount))

    recent_orders_query = (
        select(Dish.name, OrderItem.amount)
        .select_from(OrderItem)
        .join(OrderItem.order)
        .join(OrderItem.menu_item)
        .join(MenuItem.dish)
        .where(Order.client_id == user_id)
        .order_by(Order.created_at.desc(), Order.id.desc(), OrderItem.id.desc())
        .limit(RECENT_ORDER_ITEMS_LIMIT)
    )
    recent_orders = (await session.execute(recent_orders_query)).all()

    return text_template(
        {
            "user_id": user_id,
            "kitchens": kitchens,
            "dish_types": dish_types,
            "last_orders": [(name, item_amount) for name, item_amount in recent_orders],
        }
    )
