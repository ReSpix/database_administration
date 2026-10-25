from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .core import Point, SeededRNG


class OrderStatus(str, Enum):
    CREATED    = "created"
    ACCEPTED   = "accepted"
    REJECTED   = "rejected"
    READY      = "ready"
    PICKED_UP  = "picked_up"
    DELIVERED  = "delivered"
    CANCELLED  = "cancelled"


@dataclass
class Order:
    id: str
    client_id: str
    restaurant_id: str
    address: Point
    created_at: float
    status: OrderStatus = OrderStatus.CREATED
    accepted_at: Optional[float] = None
    ready_at: Optional[float] = None
    picked_up_at: Optional[float] = None
    delivered_at: Optional[float] = None
    courier_id: Optional[str] = None


@dataclass
class Client:
    id: str
    home: Point
    cancel_prob: float = 0.02


@dataclass
class Restaurant:
    id: str
    position: Point
    cuisine: str
    prep_mean: float          # секунды (среднее время готовки)
    prep_sigma: float
    capacity: int
    accept_base_prob: float = 0.95
    active_orders: int = 0

    def can_accept(self) -> bool:
        return self.active_orders < self.capacity

    def acceptance_probability(self) -> float:
        if not self.can_accept():
            return 0.05
        load = self.active_orders / self.capacity
        return self.accept_base_prob * (1.0 - 0.5 * load)

    def sample_prep_time(self, rng: SeededRNG) -> float:
        return rng.lognormal(self.prep_mean, self.prep_sigma)


@dataclass
class Courier:
    id: str
    position: Point
    speed_mean: float
    speed_sigma: float
    status: str = "IDLE"                      # IDLE | BUSY | OFFLINE | RESERVED
    route: list[dict] = field(default_factory=list)
    active_order_id: Optional[str] = None     # ← один заказ, не список
    offer_accept_base: float = 0.85
    offer_detour_penalty: float = 0.15

    def sample_speed(self, rng: "SeededRNG") -> float:
        return max(1.0, rng.normal(self.speed_mean, self.speed_sigma))

    def accept_probability(self, detour_ratio: float) -> float:
        p = self.offer_accept_base - self.offer_detour_penalty * max(0.0, detour_ratio - 1.0)
        return max(0.05, min(1.0, p))

    def advance(self, dt: float, rng: "SeededRNG") -> None:
        if not self.route:
            return
        wp = self.route[0]
        target = Point(wp["location"]["x"], wp["location"]["y"])
        self.position = self.position.move_towards(target, self.sample_speed(rng) * dt)