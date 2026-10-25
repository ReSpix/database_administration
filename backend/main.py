from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from backend.api import clients, couriers, public, restaurants
from backend.courier_locations import redis_client
from backend.database.session import engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
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