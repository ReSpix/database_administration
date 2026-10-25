
from __future__ import annotations

from decimal import Decimal

from .api import ApiClient
from .core import Point, SeededRNG
from .entities import Client, Courier, MenuItem, Restaurant


# Moscow reference point for local coordinate system (1 unit = 1 meter)
MOSCOW_LON = 37.6173
MOSCOW_LAT = 55.7558
METERS_PER_DEG_LON = 111320 * 0.57  # at Moscow latitude
METERS_PER_DEG_LAT = 111320


def meters_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Convert local meters to lon/lat."""
    lon = MOSCOW_LON + x / METERS_PER_DEG_LON
    lat = MOSCOW_LAT + y / METERS_PER_DEG_LAT
    return lon, lat


class ClientEmulator:
    def __init__(self, client: Client, api: ApiClient) -> None:
        self.client = client
        self.api = api

    async def create_order(self, restaurant: Restaurant, address: Point) -> int:
        # Pick 1-2 random menu items
        rng = getattr(self, 'rng', None)
        items = restaurant.pick_menu_items(rng, count=1) if rng else []
        if not items:
            # Fallback: create dummy items
            items = [MenuItem(id=1, dish_id=1, name="Default", price=10.0)]
        
        order_items = [
            {"menu_item_id": item.id, "quantity": 1}
            for item in items
        ]
        
        # Convert address from meters to lon/lat for API
        lon, lat = meters_to_lonlat(address.x, address.y)
        
        resp = await self.api.post("/orders", {
            "client_id": self.client.id,
            "restaurant_id": restaurant.id,
            "address": f"Address near ({address.x:.0f}, {address.y:.0f})",
            "longitude": lon,
            "latitude": lat,
            "items": order_items,
        })
        return resp["id"]

    async def cancel(self, order_id: int) -> None:
        # Backend doesn't have cancel endpoint - we just track locally
        pass


class RestaurantEmulator:
    def __init__(self, restaurant: Restaurant, api: ApiClient, rng: SeededRNG) -> None:
        self.restaurant = restaurant
        self.api = api
        self.rng = rng

    def decides_accept(self) -> bool:
        return self.rng.random() < self.restaurant.acceptance_probability()

    async def accept(self, order_id: int) -> None:
        await self.api.post(f"/restaurants/{self.restaurant.id}/orders/{order_id}/accept", {})

    async def mark_ready(self, order_id: int) -> None:
        await self.api.post(f"/restaurants/{self.restaurant.id}/orders/{order_id}/ready", {})


class CourierEmulator:
    def __init__(self, courier: Courier, api: ApiClient, rng: SeededRNG) -> None:
        self.courier = courier
        self.api = api
        self.rng = rng
        self.pending_order_id: int | None = None

    async def heartbeat(self) -> None:
        # Convert position from meters to lon/lat for API
        lon, lat = meters_to_lonlat(self.courier.position.x, self.courier.position.y)
        await self.api.put(f"/couriers/{self.courier.id}/location", {
            "longitude": lon,
            "latitude": lat,
        })

    async def poll_offers(self) -> dict | None:
        """Get available orders from backend."""
        if self.pending_order_id is not None:
            return {"order_id": self.pending_order_id}
        
        resp = await self.api.get(f"/couriers/{self.courier.id}/orders/available")
        orders = resp if isinstance(resp, list) else []
        if orders:
            # Pick the first available order
            order = orders[0]
            self.pending_order_id = order["id"]
            return order
        return None

    def decides_accept(self, order: dict) -> bool:
        """Decide whether to accept an available order."""
        # The backend returns restaurant coordinates in the order object (already in lon/lat)
        rest_x = float(order.get("longitude", 0))
        rest_y = float(order.get("latitude", 0))
        target = Point(rest_x, rest_y)
        
        # Convert courier position to lon/lat for distance calculation
        courier_lon, courier_lat = meters_to_lonlat(self.courier.position.x, self.courier.position.y)
        courier_pos_ll = Point(courier_lon, courier_lat)
        
        detour_ratio = courier_pos_ll.distance(target) / 1000.0  # km
        return self.rng.random() < self.courier.accept_probability(detour_ratio)

    async def accept_order(self, order: dict) -> None:
        oid = order["id"]
        await self.api.post(f"/couriers/{self.courier.id}/orders/{oid}/accept", {})
        self.pending_order_id = oid

    async def decline_order(self, order: dict) -> None:
        self.pending_order_id = None

    async def complete_pickup(self, order_id: int) -> None:
        await self.api.post(f"/couriers/{self.courier.id}/orders/{order_id}/pickup", {})

    async def complete_delivery(self, order_id: int) -> None:
        await self.api.post(f"/couriers/{self.courier.id}/orders/{order_id}/deliver", {})
        self.pending_order_id = None

    async def complete_waypoint(self, wp: dict) -> None:
        """Complete a waypoint (pickup or dropoff)."""
        oid = wp["order_id"]
        if wp["type"] == "pickup":
            await self.complete_pickup(oid)
        else:
            await self.complete_delivery(oid)