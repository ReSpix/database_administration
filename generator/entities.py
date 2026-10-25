from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .core import Point, SeededRNG


class OrderStatus(str, Enum):
    NEW        = "new"
    ACCEPTED   = "accepted"
    READY      = "ready"
    ASSIGNED   = "assigned"
    PICKED_UP  = "picked_up"
    DELIVERED  = "delivered"
    CANCELLED  = "cancelled"


@dataclass
class MenuItem:
    id: int
    dish_id: int
    name: str
    price: float


@dataclass
class Restaurant:
    id: int
    position: Point
    cuisine: str
    prep_mean: float          # seconds (mean prep time)
    prep_sigma: float
    capacity: int
    menu_items: list[MenuItem] = field(default_factory=list)
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

    def pick_menu_items(self, rng: SeededRNG | None, count: int = 1) -> list[MenuItem]:
        """Pick random menu items for an order."""
        if not self.menu_items or rng is None:
            return []
        k = min(count, len(self.menu_items))
        result = rng.weighted_choice(
            self.menu_items, [1.0] * len(self.menu_items), k=k
        )
        # Ensure we always return a list
        if isinstance(result, MenuItem):
            return [result]
        return result


@dataclass
class Client:
    id: int
    home: Point
    cancel_prob: float = 0.02


@dataclass
class Courier:
    id: int
    position: Point
    speed_mean: float
    speed_sigma: float
    status: str = "IDLE"                      # IDLE | BUSY | OFFLINE
    route: list[dict] = field(default_factory=list)
    active_order_id: Optional[int] = None     # one order at a time
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