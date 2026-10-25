from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from backend.api import clients, couriers, public, restaurants
from backend.courier_locations import redis_client
from backend.database.session import engine, async_session_factory
from backend.database.models import Base


async def init_seed_data() -> None:
    """Initialize database with seed data if empty."""
    from sqlalchemy import select
    from backend.database.models import (
        AuthProvider,
        Brand,
        Client,
        Courier,
        Dish,
        MenuItem,
        OrderStatus,
        Restaurant,
    )
    from decimal import Decimal

    async with async_session_factory() as session:
        # Check if already initialized
        existing_brand = await session.scalar(select(Brand).limit(1))
        if existing_brand:
            return

        print("Initializing database with seed data...")

        # Create order statuses
        statuses = ["new", "accepted", "ready", "assigned", "picked_up", "delivered"]
        for name in statuses:
            status = OrderStatus(name=name)
            session.add(status)
        await session.flush()

        # Create brand
        brand = Brand(name="Test Brand", description="Test brand for simulation")
        session.add(brand)
        await session.flush()

        # Create dishes
        dishes_data = [
            (1, "Margherita", "Classic pizza"),
            (2, "Pepperoni", "Spicy pizza"),
            (3, "Vegetarian", "Veggie pizza"),
            (4, "Four Cheese", "Cheese pizza"),
            (5, "Ribeye Steak", "Premium steak"),
            (6, "Filet Mignon", "Tender steak"),
            (7, "Burger", "Beef burger"),
            (8, "Salmon Roll", "Sushi roll"),
            (9, "Tuna Sashimi", "Fresh tuna"),
            (10, "California Roll", "Classic roll"),
            (11, "Eel Roll", "Unagi roll"),
        ]

        for dish_id, name, desc in dishes_data:
            dish = Dish(id=dish_id, brand_id=brand.id, name=name, description=desc)
            session.add(dish)
        await session.flush()

        # Create restaurants
        restaurants_data = [
            (1, brand.id, "Pizza Place", "123 Main St", Decimal("37.6173"), Decimal("55.7558")),
            (2, brand.id, "Steak House", "456 Oak Ave", Decimal("37.6200"), Decimal("55.7600")),
            (3, brand.id, "Sushi Bar", "789 Pine Rd", Decimal("37.6250"), Decimal("55.7650")),
        ]

        for rest_id, brand_id, name, addr, lon, lat in restaurants_data:
            rest = Restaurant(
                id=rest_id,
                brand_id=brand_id,
                address=addr,
                longitude=lon,
                latitude=lat,
            )
            session.add(rest)
        await session.flush()

        # Create menu items
        menu_items_data = [
            (1, 1, 1, Decimal("12.50")),
            (2, 1, 2, Decimal("14.00")),
            (3, 1, 3, Decimal("13.00")),
            (4, 1, 4, Decimal("15.00")),
            (5, 2, 5, Decimal("25.00")),
            (6, 2, 6, Decimal("30.00")),
            (7, 2, 7, Decimal("18.00")),
            (8, 3, 8, Decimal("10.00")),
            (9, 3, 9, Decimal("16.00")),
            (10, 3, 10, Decimal("12.00")),
            (11, 3, 11, Decimal("14.00")),
        ]

        for mi_id, rest_id, dish_id, price in menu_items_data:
            mi = MenuItem(id=mi_id, restaurant_id=rest_id, dish_id=dish_id, price=price)
            session.add(mi)

        # Create auth provider
        auth_provider = AuthProvider(name="test")
        session.add(auth_provider)
        await session.flush()

        # Create clients
        for i in range(1, 21):
            client = Client(id=i, auth_provider_id=auth_provider.id, external_id=f"client_{i}")
            session.add(client)

        # Create couriers
        for i in range(1, 16):
            courier = Courier(id=i, name=f"Courier {i}")
            session.add(courier)

        await session.commit()
        print("Database seed data initialized successfully!")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Create tables if they don't exist
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Initialize seed data
    await init_seed_data()
    
    try:
        yield
    finally:
        try:
            await redis_client.aclose()
        finally:
            await engine.dispose()


app = FastAPI(title="Food Delivery API", lifespan=lifespan)
app.include_router(public.router)
app.include_router(clients.router)
app.include_router(restaurants.router)
app.include_router(couriers.router)


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}