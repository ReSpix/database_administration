from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import Order, OrderStatus
from backend.database.session import get_db_session

router = APIRouter(prefix="/clients/{client_id}", tags=["clients"])


@router.get("/orders/{order_id}/status")
async def get_order_status(
    client_id: int,
    order_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    result = await session.execute(
        select(Order.id, OrderStatus.name, Order.created_at, Order.estimated_ready_at)
        .join(OrderStatus, Order.status_id == OrderStatus.id)
        .where(Order.id == order_id, Order.client_id == client_id)
    )
    order = result.one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    return {
        "order_id": order.id,
        "status": order.name,
        "created_at": order.created_at,
        "estimated_ready_at": order.estimated_ready_at,
    }
