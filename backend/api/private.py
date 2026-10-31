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
    Courier
)
from backend.database.session import get_db_session
import asyncio

from ..courier_locations import get_couriers_location

router = APIRouter(tags=["private"])


@router.get("/orders")
async def list_orders(
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, object]]:
    orders = (await session.scalars(select(Order).order_by(Order.id))).all()
    return [
        {
            "id": order.id, 
            "client_id": order.client_id, 
            "status_id": order.status_id,
            "address": order.address,
            "longitude": order.longitude,
            "latitude": order.latitude,
            "created_at": order.created_at,
            "estimated_ready_at": order.estimated_ready_at,
            "finished_at": order.finished_at,
        }
        for order in orders
    ]

@router.get("/clients")
async def list_clients(
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, object]]:
    clients = (await session.scalars(select(Client).order_by(Client.id))).all()
    return [
        {
            "id": client.id, 
            "auth_provider_id": client.auth_provider_id, 
            "external_id": client.external_id,
        }
        for client in clients
    ]


@router.get("/couriers")
async def list_couriers(
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, object]]:
    couriers = (await session.scalars(select(Courier).order_by(Courier.id))).all()
    locations = await get_couriers_location([courier.id for courier in couriers])
    return [
        {
            "id": courier.id, 
            "name": courier.name,
            "location": location,
        }
        for courier, location in zip(couriers, locations)
    ]