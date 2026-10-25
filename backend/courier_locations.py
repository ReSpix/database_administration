import os

from redis.asyncio import Redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
COURIER_LOCATIONS_KEY = "courier_locations"

redis_client = Redis.from_url(REDIS_URL, decode_responses=True)


async def save_courier_location(
    courier_id: int,
    longitude: float,
    latitude: float,
) -> None:
    # Redis 8.x expects: geoadd(key, (longitude, latitude, member))
    await redis_client.geoadd(
        COURIER_LOCATIONS_KEY,
        (longitude, latitude, str(courier_id)),
    )


async def find_nearby_couriers(
    longitude: float,
    latitude: float,
    radius_km: float,
    limit: int = 10,
) -> list[str]:
    if radius_km <= 0:
        raise ValueError("radius_km must be greater than zero")
    if limit <= 0:
        raise ValueError("limit must be greater than zero")

    return await redis_client.geosearch(
        COURIER_LOCATIONS_KEY,
        longitude=longitude,
        latitude=latitude,
        radius=radius_km,
        unit="km",
        sort="ASC",
        count=limit,
    )
