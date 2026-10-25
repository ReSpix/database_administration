from __future__ import annotations

import statistics
from dataclasses import dataclass, field


@dataclass
class Metrics:
    orders_created:   int = 0
    orders_accepted:  int = 0
    orders_rejected:  int = 0
    orders_delivered: int = 0
    offer_accepts:    int = 0
    offer_declines:   int = 0

    delivery_times:    list[float] = field(default_factory=list)
    prep_times:        list[float] = field(default_factory=list)
    ready_to_pickup:   list[float] = field(default_factory=list)
    batch_sizes:       list[int]   = field(default_factory=list)

    def summary(self) -> dict:
        def stats(xs: list[float]) -> dict:
            if not xs:
                return {"count": 0}
            s = sorted(xs)
            return {
                "count": len(xs),
                "mean":  round(statistics.mean(xs), 1),
                "p50":   round(s[len(s) // 2], 1),
                "p95":   round(s[int(len(s) * 0.95)], 1),
                "p99":   round(s[int(len(s) * 0.99)], 1),
            }

        return {
            "orders_created":   self.orders_created,
            "orders_accepted":  self.orders_accepted,
            "orders_rejected":  self.orders_rejected,
            "orders_delivered": self.orders_delivered,
            "offer_accepts":    self.offer_accepts,
            "offer_declines":   self.offer_declines,
            "accept_rate":      round(self.orders_accepted / max(1, self.orders_created), 3),
            "delivery_time_sec":   stats(self.delivery_times),
            "prep_time_sec":       stats(self.prep_times),
            "ready_to_pickup_sec": stats(self.ready_to_pickup),
            "batch_size":          stats(self.batch_sizes),
        }