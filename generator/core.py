from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass, field
from enum import Enum, auto


@dataclass(frozen=True)
class Point:
    x: float
    y: float

    def distance(self, other: "Point") -> float:
        return math.hypot(self.x - other.x, self.y - other.y)

    def move_towards(self, target: "Point", dist: float) -> "Point":
        d = self.distance(target)
        if d == 0 or d <= dist:
            return target
        f = dist / d
        return Point(self.x + (target.x - self.x) * f,
                     self.y + (target.y - self.y) * f)


class EventType(Enum):
    ORDER_ARRIVAL      = auto()
    RESTAURANT_DECISION = auto()
    PREP_DONE          = auto()
    COURIER_TICK       = auto()
    OFFER_DECISION     = auto()
    STOP               = auto()


@dataclass(order=True)
class Event:
    time: float
    seq: int
    type: EventType = field(compare=False)
    payload: dict = field(compare=False, default_factory=dict)


class EventQueue:
    """Min-heap с детерминированным tiebreak через seq."""
    def __init__(self) -> None:
        self._heap: list[Event] = []
        self._seq = 0

    def push(self, time: float, type_: EventType, payload: dict | None = None) -> None:
        self._seq += 1
        heapq.heappush(self._heap, Event(time, self._seq, type_, payload or {}))

    def pop(self) -> Event:
        return heapq.heappop(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)


class SeededRNG:
    """Один seed → воспроизводимый прогон."""
    def __init__(self, seed: int) -> None:
        self._r = random.Random(seed)

    def exponential(self, rate: float) -> float:
        return self._r.expovariate(rate)

    def lognormal(self, mean: float, sigma: float) -> float:
        mu = math.log(mean) - 0.5 * sigma * sigma
        return self._r.lognormvariate(mu, sigma)

    def normal(self, mean: float, sigma: float) -> float:
        return self._r.gauss(mean, sigma)

    def uniform(self, a: float, b: float) -> float:
        return self._r.uniform(a, b)

    def random(self) -> float:
        return self._r.random()

    def weighted_choice(self, items, weights):
        return self._r.choices(items, weights=weights, k=1)[0]