from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import Order, OrderStatus

NEW = "new"
ACCEPTED = "accepted"
READY = "ready"
ASSIGNED = "assigned"
PICKED_UP = "picked_up"
DELIVERED = "delivered"


async def get_status_id(session: AsyncSession, name: str) -> int:
    status_id = await session.scalar(
        select(OrderStatus.id)
        .where(OrderStatus.name == name)
        .order_by(OrderStatus.id)
        .limit(1)
    )
    if status_id is not None:
        return status_id

    status = OrderStatus(name=name)
    session.add(status)
    await session.flush()
    return status.id


async def set_order_status(
    session: AsyncSession,
    order: Order,
    name: str,
) -> None:
    order.status_id = await get_status_id(session, name)
