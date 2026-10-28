
from __future__ import annotations

from decimal import Decimal
import logging

from .api import ApiClient
from .core import Point, SeededRNG
from .entities import Client, Courier, MenuItem, Restaurant

log = logging.getLogger("delivery_sim.emulator")


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
        self._log = logging.getLogger("delivery_sim.emulator.client")

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
        self._log = logging.getLogger("delivery_sim.emulator.restaurant")

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
        self.pending_offer: dict | None = None
        self.declined_offers: set[int] = set()  # Track recently declined offers
        self._log = logging.getLogger("delivery_sim.emulator.courier")

    async def heartbeat(self) -> None:
        # Convert position from meters to lon/lat for API
        lon, lat = meters_to_lonlat(self.courier.position.x, self.courier.position.y)
        await self.api.put(f"/couriers/{self.courier.id}/location", {
            "longitude": lon,
            "latitude": lat,
        })

    async def poll_offers(self) -> dict | None:
        """Get available orders from backend."""
        # If we already have a pending offer, return it (don't poll again)
        if self.pending_offer is not None:
            return self.pending_offer
        
        resp = await self.api.get(f"/couriers/{self.courier.id}/orders/available")
        orders = resp if isinstance(resp, list) else []
        if orders:
            # Pick the first available order that we haven't declined
            for order in orders:
                oid = order["id"]
                if oid not in self.declined_offers:
                    self.pending_offer = {"id": oid}
                    return self.pending_offer
            # All available orders were declined - don't offer them again
            # Return None to indicate no acceptable offers
            return None
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
        # Always accept for testing
        return True

    async def accept_order(self, order: dict) -> None:
        oid = order["id"]
        try:
            self._log.info(f"ACCEPT_API courier_id={self.courier.id} accepting order_id={oid}")
            resp = await self.api.post(f"/couriers/{self.courier.id}/orders/{oid}/accept", {})
            self._log.info(f"ACCEPT_RESP courier_id={self.courier.id} response={resp}")
            # Clear pending offer after accepting
            self.pending_offer = None
            self.declined_offers.discard(oid)
        except Exception as e:
            self._log.error(f"ACCEPT_ERROR courier_id={self.courier.id} error={e}")
            if "409" in str(e):
                self.declined_offers.add(oid)
                self.pending_offer = None
                raise
            raise

    async def decline_order(self, order: dict) -> None:
        oid = order["id"]
        self._log.debug("DECLINE courier_id=%s order_id=%s", self.courier.id, oid)
        self.declined_offers.add(oid)
        # Clear pending offer after declining
        self.pending_offer = None

    async def complete_pickup(self, order_id: int) -> None:
        await self.api.post(f"/couriers/{self.courier.id}/orders/{order_id}/pickup", {})

    async def complete_delivery(self, order_id: int) -> None:
        await self.api.post(f"/couriers/{self.courier.id}/orders/{order_id}/deliver", {})
        self.pending_offer = None
        # Clear declined offers when order is delivered
        self.declined_offers.discard(order_id)

    async def complete_waypoint(self, wp: dict) -> None:
        """Complete a waypoint (pickup or dropoff)."""
        oid = wp["order_id"]
        if wp["type"] == "pickup":
            await self.complete_pickup(oid)
        else:
            await self.complete_delivery(oid)