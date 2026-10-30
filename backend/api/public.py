from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.schemas import OrderCreate
from backend.api.statuses import NEW, get_status_id
from backend.database.models import (
    Brand,
    Client,
    Dish,
    MenuItem,
    Order,
    OrderItem,
    Restaurant,
    StopList,
)
from backend.database.session import get_db_session

router = APIRouter(tags=["catalog"])


@router.get("/brands")
async def list_brands(
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, object]]:
    brands = (await session.scalars(select(Brand).order_by(Brand.name))).all()
    return [
        {"id": brand.id, "name": brand.name, "description": brand.description}
        for brand in brands
    ]


@router.get("/restaurants")
async def list_restaurants(
    brand_id: int | None = Query(default=None, gt=0),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, object]]:
    query = select(Restaurant).order_by(Restaurant.id)
    if brand_id is not None:
        query = query.where(Restaurant.brand_id == brand_id)
    restaurants = (await session.scalars(query)).all()
    return [
        {
            "id": restaurant.id,
            "brand_id": restaurant.brand_id,
            "address": restaurant.address,
            "longitude": restaurant.longitude,
            "latitude": restaurant.latitude,
        }
        for restaurant in restaurants
    ]


@router.get("/dishes")
async def list_dishes(
    brand_id: int | None = Query(default=None, gt=0),
    restaurant_id: int | None = Query(default=None, gt=0),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, object]]:
    now = func.now()
    active_stop_list = select(StopList.id).where(
        StopList.menu_item_id == MenuItem.id,
        or_(StopList.until.is_(None), StopList.until > now),
    )
    query = (
        select(Dish, MenuItem)
        .join(MenuItem, MenuItem.dish_id == Dish.id)
        .where(~active_stop_list.exists())
        .order_by(Dish.id, MenuItem.restaurant_id)
    )
    if brand_id is not None:
        query = query.where(Dish.brand_id == brand_id)
    if restaurant_id is not None:
        query = query.where(MenuItem.restaurant_id == restaurant_id)
    rows = (await session.execute(query)).all()
    return [
        {
            "id": dish.id,
            "brand_id": dish.brand_id,
            "name": dish.name,
            "description": dish.description,
            "restaurant_id": menu_item.restaurant_id,
            "menu_item_id": menu_item.id,
            "price": menu_item.price,
        }
        for dish, menu_item in rows
    ]


@router.post("/orders", status_code=201)
async def create_order(
    order_data: OrderCreate,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    client = await session.get(Client, order_data.client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    order_dishes_amount = sum(dish.quantity for dish in order_data.items)

    if order_dishes_amount > 99:
        raise HTTPException(
            status_code=400,
            detail="Maximum 99 dishes in one order",
        )

    restaurant = await session.get(Restaurant, order_data.restaurant_id)
    if restaurant is None:
        raise HTTPException(status_code=404, detail="Restaurant not found")

    menu_item_ids = [item.menu_item_id for item in order_data.items]
    if len(set(menu_item_ids)) != len(menu_item_ids):
        raise HTTPException(status_code=422, detail="Duplicate menu_item_id")

    active_stop_list = select(StopList.id).where(
        StopList.menu_item_id == MenuItem.id,
        or_(StopList.until.is_(None), StopList.until > func.now()),
    )
    menu_items = (
        await session.scalars(
            select(MenuItem).where(
                MenuItem.id.in_(menu_item_ids),
                MenuItem.restaurant_id == restaurant.id,
                ~active_stop_list.exists(),
            )
        )
    ).all()
    menu_items_by_id = {menu_item.id: menu_item for menu_item in menu_items}
    if len(menu_items_by_id) != len(menu_item_ids):
        raise HTTPException(
            status_code=422,
            detail="One or more menu items are unavailable for this restaurant",
        )

    status_id = await get_status_id(session, NEW)
    order = Order(
        client_id=client.id,
        status_id=status_id,
        restaurant_id=restaurant.id,
        address=order_data.address,
        longitude=order_data.longitude,
        latitude=order_data.latitude,
    )
    session.add(order)
    await session.flush()

    total = 0
    for item in order_data.items:
        menu_item = menu_items_by_id[item.menu_item_id]
        item_total = menu_item.price * item.quantity
        total += item_total
        session.add(
            OrderItem(
                order_id=order.id,
                menu_item_id=menu_item.id,
                amount=item.quantity,
                total_cost=item_total,
            )
        )

    await session.commit()
    return {
        "id": order.id,
        "status": NEW,
        "restaurant_id": order.restaurant_id,
        "total": total,
    }
