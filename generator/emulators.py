
from __future__ import annotations

from .api import ApiClient
from .core import Point, SeededRNG
from .entities import Client, Courier, Restaurant


class ClientEmulator:
    def __init__(self, client: Client, api: ApiClient) -> None:
        self.client = client
        self.api = api

    async def create_order(self, restaurant: Restaurant, address: Point) -> str:
        resp = await self.api.post("/v1/orders", {
            "clientId": self.client.id,
            "restaurantId": restaurant.id,
            "address": {"x": address.x, "y": address.y},
        })
        return resp["orderId"]

    async def cancel(self, order_id: str) -> None:
        await self.api.post(f"/v1/orders/{order_id}/cancel", {})


class RestaurantEmulator:
    def __init__(self, restaurant: Restaurant, api: ApiClient, rng: SeededRNG) -> None:
        self.restaurant = restaurant
        self.api = api
        self.rng = rng

    def decides_accept(self) -> bool:
        return self.rng.random() < self.restaurant.acceptance_probability()

    async def accept(self, order_id: str) -> None:
        await self.api.post(f"/v1/restaurant/orders/{order_id}/accept", {})

    async def reject(self, order_id: str, reason: str = "too_busy") -> None:
        await self.api.post(f"/v1/restaurant/orders/{order_id}/reject", {"reason": reason})

    async def mark_ready(self, order_id: str) -> None:
        await self.api.post(f"/v1/restaurant/orders/{order_id}/ready", {})


class CourierEmulator:
    def __init__(self, courier: Courier, api: ApiClient, rng: SeededRNG) -> None:
        self.courier = courier
        self.api = api
        self.rng = rng
        self.pending_offer: dict | None = None

    async def heartbeat(self) -> None:
        await self.api.post(f"/v1/couriers/{self.courier.id}/status", {
            "status": self.courier.status,
            "position": {"x": self.courier.position.x, "y": self.courier.position.y},
        })

    async def poll_offers(self) -> dict | None:
        # если оффер уже есть — не опрашиваем, ждём решения
        if self.pending_offer is not None:
            return self.pending_offer
        resp = await self.api.get(f"/v1/couriers/{self.courier.id}/offers")
        offers = resp.get("offers") or []
        self.pending_offer = offers[0] if offers else None
        return self.pending_offer

    def decides_accept(self, offer: dict) -> bool:
        wp = offer["route"][0]["location"]
        target = Point(wp["x"], wp["y"])
        detour_ratio = self.courier.position.distance(target) / 1000.0
        return self.rng.random() < self.courier.accept_probability(detour_ratio)

    async def accept_offer(self, offer: dict) -> None:
        await self.api.post(f"/v1/couriers/{self.courier.id}/offers/{offer['id']}/accept", {})

    async def decline_offer(self, offer: dict) -> None:
        await self.api.post(f"/v1/couriers/{self.courier.id}/offers/{offer['id']}/decline", {})

    async def complete_waypoint(self, wp: dict) -> None:
        oid, cid = wp["orderId"], self.courier.id
        action = "complete-pickup" if wp["type"] == "pickup" else "complete-delivery"
        await self.api.post(f"/v1/couriers/{cid}/tasks/{oid}/{action}", {})