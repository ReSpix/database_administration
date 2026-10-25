from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.schemas import MenuItemCreate, MenuItemUpdate, OrderReadyUpdate
from backend.api.statuses import ACCEPTED, NEW, READY, set_order_status
from backend.courier_locations import find_nearby_couriers
from backend.database.models import (
    Dish,
    MenuItem,
    Order,
    OrderStatus,
    Restaurant,
    StopList,
)
from backend.database.session import get_db_session

router = APIRouter(prefix="/restaurants/{restaurant_id}", tags=["restaurants"])


async def require_restaurant(session: AsyncSession, restaurant_id: int) -> Restaurant:
    restaurant = await session.get(Restaurant, restaurant_id)
    if restaurant is None:
        raise HTTPException(status_code=404, detail="Restaurant not found")
    return restaurant


@router.get("/couriers/nearby")
async def list_nearby_couriers(
    restaurant_id: int,
    radius_km: float = Query(default=5, gt=0),
    limit: int = Query(default=10, gt=0, le=100),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, list[str]]:
    restaurant = await require_restaurant(session, restaurant_id)
    courier_ids = await find_nearby_couriers(
        longitude=float(restaurant.longitude),
        latitude=float(restaurant.latitude),
        radius_km=radius_km,
        limit=limit,
    )
    return {"courier_ids": courier_ids}


@router.get("/menu-items")
async def list_menu_items(
    restaurant_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, object]]:
    await require_restaurant(session, restaurant_id)
    active_stop_list = select(StopList.id).where(
        StopList.menu_item_id == MenuItem.id,
        or_(StopList.until.is_(None), StopList.until > func.now()),
    )
    rows = (
        await session.execute(
            select(MenuItem, Dish, ~active_stop_list.exists())
            .join(Dish, Dish.id == MenuItem.dish_id)
            .where(MenuItem.restaurant_id == restaurant_id)
            .order_by(MenuItem.id)
        )
    ).all()
    return [
        {
            "id": menu_item.id,
            "dish_id": dish.id,
            "dish_name": dish.name,
            "price": menu_item.price,
            "available": is_available,
        }
        for menu_item, dish, is_available in rows
    ]


@router.post("/menu-items", status_code=status.HTTP_201_CREATED)
async def add_menu_item(
    restaurant_id: int,
    menu_item_data: MenuItemCreate,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    restaurant = await require_restaurant(session, restaurant_id)
    dish = await session.get(Dish, menu_item_data.dish_id)
    if dish is None:
        raise HTTPException(status_code=404, detail="Dish not found")
    if dish.brand_id != restaurant.brand_id:
        raise HTTPException(
            status_code=422,
            detail="Dish does not belong to the restaurant's brand",
        )

    menu_item = MenuItem(
        restaurant_id=restaurant.id,
        dish_id=dish.id,
        price=menu_item_data.price,
    )
    session.add(menu_item)
    await session.commit()
    await session.refresh(menu_item)
    return {
        "id": menu_item.id,
        "restaurant_id": menu_item.restaurant_id,
        "dish_id": menu_item.dish_id,
        "price": menu_item.price,
        "available": True,
    }


@router.patch("/menu-items/{menu_item_id}")
async def update_menu_item(
    restaurant_id: int,
    menu_item_id: int,
    menu_item_data: MenuItemUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    changes = menu_item_data.model_dump(exclude_unset=True)
    if not changes or any(value is None for value in changes.values()):
        raise HTTPException(
            status_code=422,
            detail="At least one valid field is required",
        )

    menu_item = await session.scalar(
        select(MenuItem).where(
            MenuItem.id == menu_item_id,
            MenuItem.restaurant_id == restaurant_id,
        )
    )
    if menu_item is None:
        raise HTTPException(status_code=404, detail="Menu item not found")

    active_stops = (
        await session.scalars(
            select(StopList).where(
                StopList.menu_item_id == menu_item.id,
                or_(StopList.until.is_(None), StopList.until > func.now()),
            )
        )
    ).all()
    if "price" in changes:
        menu_item.price = changes["price"]
    if "available" in changes:
        now = datetime.now(UTC)
        if changes["available"]:
            for stop in active_stops:
                stop.until = now
        elif not active_stops:
            session.add(
                StopList(
                    menu_item_id=menu_item.id,
                    reason="Unavailable",
                    stopped_at=now,
                )
            )

    await session.commit()
    is_available = (
        changes["available"]
        if "available" in changes
        else not active_stops
    )
    return {
        "id": menu_item.id,
        "restaurant_id": menu_item.restaurant_id,
        "dish_id": menu_item.dish_id,
        "price": menu_item.price,
        "available": is_available,
    }


async def get_restaurant_order(
    session: AsyncSession,
    restaurant_id: int,
    order_id: int,
) -> Order:
    order = await session.scalar(
        select(Order)
        .where(Order.id == order_id, Order.restaurant_id == restaurant_id)
        .with_for_update()
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.post("/orders/{order_id}/accept")
async def accept_order(
    restaurant_id: int,
    order_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    await require_restaurant(session, restaurant_id)
    order = await get_restaurant_order(session, restaurant_id, order_id)
    current_status = await session.get(OrderStatus, order.status_id)
    if current_status is None or current_status.name != NEW:
        raise HTTPException(
            status_code=409,
            detail="Order is not awaiting acceptance",
        )

    await set_order_status(session, order, ACCEPTED)
    await session.commit()
    return {"order_id": order.id, "status": ACCEPTED}


@router.post("/orders/{order_id}/ready")
async def mark_order_ready(
    restaurant_id: int,
    order_id: int,
    ready_data: OrderReadyUpdate | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    await require_restaurant(session, restaurant_id)
    order = await get_restaurant_order(session, restaurant_id, order_id)
    current_status = await session.get(OrderStatus, order.status_id)
    if current_status is None or current_status.name != ACCEPTED:
        raise HTTPException(status_code=409, detail="Order has not been accepted")
    estimated_ready_at = (
        ready_data.estimated_ready_at if ready_data is not None else None
    )
    if estimated_ready_at is not None and estimated_ready_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=422,
            detail="estimated_ready_at must be in the future",
        )

    order.estimated_ready_at = estimated_ready_at
    await set_order_status(session, order, READY)
    await session.commit()
    return {
        "order_id": order.id,
        "status": READY,
        "estimated_ready_at": order.estimated_ready_at,
    }
