from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.courier_locations import save_courier_location
from backend.api.schemas import CourierLocationUpdate
from backend.api.statuses import ASSIGNED, DELIVERED, PICKED_UP, READY, set_order_status
from backend.database.models import (
    Courier,
    Order,
    OrderAssignment,
    OrderStatus,
    Restaurant,
)
from backend.database.session import get_db_session

router = APIRouter(prefix="/couriers/{courier_id}", tags=["couriers"])


async def require_courier(session: AsyncSession, courier_id: int) -> Courier:
    courier = await session.get(Courier, courier_id)
    if courier is None:
        raise HTTPException(status_code=404, detail="Courier not found")
    return courier


@router.put("/location")
async def update_courier_location(
    courier_id: int,
    location: CourierLocationUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    await require_courier(session, courier_id)
    await save_courier_location(
        courier_id,
        longitude=float(location.longitude),
        latitude=float(location.latitude),
    )
    return {"courier_id": courier_id, "status": "location_updated"}


@router.get("/orders/available")
async def list_available_orders(
    courier_id: int,
    session: AsyncSession = Depends(get_db_session),
    limit: int | None = None
) -> list[dict[str, object]]:
    if limit is None:
        limit = 100
    
    await require_courier(session, courier_id)
    assigned_order = select(OrderAssignment.id).where(
        OrderAssignment.order_id == Order.id
    )
    rows = (
        await session.execute(
            select(Order, Restaurant)
            .join(Restaurant, Restaurant.id == Order.restaurant_id)
            .join(OrderStatus, OrderStatus.id == Order.status_id)
            .where(
                OrderStatus.name == READY,
                ~assigned_order.exists(),
            )
            .order_by(Order.created_at, Order.id)
            .limit(int(limit))
        )
    ).all()
    return [
        {
            "id": order.id,
            "restaurant_id": order.restaurant_id,
            "restaurant_address": restaurant.address,
            "address": order.address,
            "longitude": order.longitude,
            "latitude": order.latitude,
            "estimated_ready_at": order.estimated_ready_at,
            "created_at": order.created_at,
        }
        for order, restaurant in rows
    ]


async def get_order_for_update(
    session: AsyncSession,
    order_id: int,
) -> Order:
    order = await session.scalar(
        select(Order).where(Order.id == order_id).with_for_update()
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


async def get_assignment(
    session: AsyncSession,
    order_id: int,
    courier_id: int,
) -> OrderAssignment:
    assignment = await session.scalar(
        select(OrderAssignment).where(
            OrderAssignment.order_id == order_id,
            OrderAssignment.courier_id == courier_id,
        )
    )
    if assignment is None:
        raise HTTPException(
            status_code=409,
            detail="Order is not assigned to this courier",
        )
    return assignment


@router.post("/orders/{order_id}/accept")
async def accept_delivery(
    courier_id: int,
    order_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    await require_courier(session, courier_id)
    order = await get_order_for_update(session, order_id)
    current_status = await session.get(OrderStatus, order.status_id)
    if current_status is None or current_status.name != READY:
        raise HTTPException(
            status_code=409,
            detail="Order is not available for delivery",
        )

    assignment_exists = await session.scalar(
        select(OrderAssignment.id).where(OrderAssignment.order_id == order.id)
    )
    if assignment_exists is not None:
        raise HTTPException(status_code=409, detail="Order has already been assigned")

    assignment = OrderAssignment(
        order_id=order.id,
        courier_id=courier_id,
        assigned_at=datetime.now(UTC),
    )
    session.add(assignment)
    await set_order_status(session, order, ASSIGNED)
    await session.commit()
    return {"order_id": order.id, "courier_id": courier_id, "status": ASSIGNED}


@router.post("/orders/{order_id}/pickup")
async def pickup_order(
    courier_id: int,
    order_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    await require_courier(session, courier_id)
    order = await get_order_for_update(session, order_id)
    assignment = await get_assignment(session, order.id, courier_id)
    current_status = await session.get(OrderStatus, order.status_id)
    if current_status is None or current_status.name != ASSIGNED:
        raise HTTPException(status_code=409, detail="Order is not awaiting pickup")

    assignment.picked_up_at = datetime.now(UTC)
    await set_order_status(session, order, PICKED_UP)
    await session.commit()
    return {"order_id": order.id, "status": PICKED_UP}


@router.post("/orders/{order_id}/deliver")
async def deliver_order(
    courier_id: int,
    order_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    await require_courier(session, courier_id)
    order = await get_order_for_update(session, order_id)
    await get_assignment(session, order.id, courier_id)
    current_status = await session.get(OrderStatus, order.status_id)
    if current_status is None or current_status.name != PICKED_UP:
        raise HTTPException(status_code=409, detail="Order has not been picked up")

    order.finished_at = datetime.now(UTC)
    await set_order_status(session, order, DELIVERED)
    await session.commit()
    return {"order_id": order.id, "status": DELIVERED}
