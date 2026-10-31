#!/usr/bin/env python3
"""Initialize database with test data for simulation."""

import os
import asyncio
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import (
    AuthProvider,
    Base,
    Brand,
    Client,
    Courier,
    Dish,
    MenuItem,
    OrderStatus,
    Restaurant,
)
from backend.database.session import async_session_factory, engine

SCHEMA_ONLY = os.getenv("INIT_DB_SCHEMA_ONLY") == "1"

async def init_db() -> None:
    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    if SCHEMA_ONLY:
        return
    
    async with async_session_factory() as session:
        # Check if already initialized
        existing_brand = await session.scalar(select(Brand).limit(1))
        if existing_brand:
            print("Database already initialized, skipping...")
            return

        print("Initializing database...")

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
            ("Margherita", "Classic pizza"),
            ("Pepperoni", "Spicy pizza"),
            ("Vegetarian", "Veggie pizza"),
            ("Four Cheese", "Cheese pizza"),
            ("Ribeye Steak", "Premium steak"),
            ("Filet Mignon", "Tender steak"),
            ("Burger", "Beef burger"),
            ("Salmon Roll", "Sushi roll"),
            ("Tuna Sashimi", "Fresh tuna"),
            ("California Roll", "Classic roll"),
            ("Eel Roll", "Unagi roll"),
        ]
        
        dishes = {}
        for name, desc in dishes_data:
            dish = Dish(brand_id=brand.id, name=name, description=desc)
            session.add(dish)
            await session.refresh(dish)
            dishes[dish.id] = dish
            await session.flush()

        # Create restaurants
        restaurants_data = [
            (brand.id, "Pizza Place", "123 Main St", Decimal("37.6173"), Decimal("55.7558")),
            (brand.id, "Steak House", "456 Oak Ave", Decimal("37.6200"), Decimal("55.7600")),
            (brand.id, "Sushi Bar", "789 Pine Rd", Decimal("37.6250"), Decimal("55.7650")),
        ]

        restaurants = {}
        for brand_id, name, addr, lon, lat in restaurants_data:
            rest = Restaurant(
                brand_id=brand_id,
                address=addr,
                longitude=lon,
                latitude=lat,
            )
            session.add(rest)
            await session.refresh(rest)
            restaurants[rest.id] = rest
            await session.flush()

        # Create menu items
        menu_items_data = [
            # Restaurant 1 (Pizza)
            (1, 1, Decimal("12.50")),
            (1, 2, Decimal("14.00")),
            (1, 3, Decimal("13.00")),
            (1, 4, Decimal("15.00")),
            # Restaurant 2 (Steak)
            (2, 5, Decimal("25.00")),
            (2, 6, Decimal("30.00")),
            (2, 7, Decimal("18.00")),
            # Restaurant 3 (Sushi)
            (3, 8, Decimal("10.00")),
            (3, 9, Decimal("16.00")),
            (3, 10, Decimal("12.00")),
            (3, 11, Decimal("14.00")),
        ]

        for rest_id, dish_id, price in menu_items_data:
            mi = MenuItem(
                restaurant_id=rest_id,
                dish_id=dish_id,
                price=price,
            )
            session.add(mi)
            await session.flush()
            await session.refresh(mi)

        # Create auth provider
        auth_provider = AuthProvider(name="test")
        session.add(auth_provider)
        await session.flush()
        
        # Create clients
        for i in range(1, 21):
            client = Client(
                auth_provider_id=auth_provider.id,
                external_id=f"client_{i}",
            )
            session.add(client)
            await session.flush()

        # Create couriers
        for i in range(1, 16):
            courier = Courier(name=f"Courier {i}")
            session.add(courier)
            await session.flush()

        await session.commit()
        print("Database initialized successfully!")


if __name__ == "__main__":
    asyncio.run(init_db())