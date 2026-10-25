from __future__ import annotations

import asyncio

from .api import ApiClient
from .core import Event, EventQueue, EventType, Point, SeededRNG
from .emulators import ClientEmulator, CourierEmulator, RestaurantEmulator
from .entities import Client, Courier, MenuItem, OrderStatus, Restaurant
from .metrics import Metrics

ARRIVAL_THRESHOLD_M = 5.0


class SimulationEngine:
    def __init__(
        self,
        api: ApiClient,
        clients: list[Client],
        restaurants: list[Restaurant],
        couriers: list[Courier],
        rng: SeededRNG,
        *,
        duration: float = 3600.0,
        order_rate: float = 0.2,
        courier_tick: float = 2.0,
    ) -> None:
        self.api = api
        self.rng = rng
        self.duration = duration
        self.order_rate = order_rate
        self.courier_tick = courier_tick

        self.clients_by_id     = {c.id: c for c in clients}
        self.restaurants_by_id = {r.id: r for r in restaurants}
        self.couriers_by_id    = {c.id: c for c in couriers}

        self.client_emus = {c.id: ClientEmulator(c, api) for c in clients}
        # Pass rng to client emulators for menu item picking
        for emu in self.client_emus.values():
            emu.rng = rng
        self.rest_emus   = {r.id: RestaurantEmulator(r, api, rng) for r in restaurants}
        self.cour_emus   = {c.id: CourierEmulator(c, api, rng) for c in couriers}

        self.orders: dict[int, dict] = {}
        self.clock = 0.0
        self.queue = EventQueue()
        self.metrics = Metrics()

    # ------------------------------------------------------------------
    async def run(self) -> Metrics:
        self._schedule_next_arrival(0.0)
        for c in self.couriers_by_id.values():
            self.queue.push(self.rng.uniform(0, self.courier_tick),
                            EventType.COURIER_TICK, {"courier_id": c.id})
        self.queue.push(self.duration, EventType.STOP)

        while self.queue:
            ev = self.queue.pop()
            self.clock = ev.time
            if ev.type == EventType.STOP:
                break
            try:
                await self._handle(ev)
            except Exception as e:                    # noqa: BLE001
                print(f"[sim] error handling {ev.type.name}: {e}")

        return self.metrics

    def _schedule_next_arrival(self, t: float) -> None:
        self.queue.push(t + self.rng.exponential(self.order_rate),
                        EventType.ORDER_ARRIVAL)

    async def _handle(self, ev: Event) -> None:
        if ev.type == EventType.ORDER_ARRIVAL:
            await self._on_order_arrival()
        elif ev.type == EventType.RESTAURANT_DECISION:
            await self._on_restaurant_decision(ev.payload["order_id"])
        elif ev.type == EventType.PREP_DONE:
            await self._on_prep_done(ev.payload["order_id"])
        elif ev.type == EventType.COURIER_TICK:
            await self._on_courier_tick(ev.payload["courier_id"])
        elif ev.type == EventType.OFFER_DECISION:
            await self._on_offer_decision(ev.payload["courier_id"])

    # ------------------------------------------------------------------
    async def _on_order_arrival(self) -> None:
        self._schedule_next_arrival(self.clock)

        client = self.rng.weighted_choice(
            list(self.clients_by_id.values()), [1.0] * len(self.clients_by_id)
        )
        rest = self.rng.weighted_choice(
            list(self.restaurants_by_id.values()), [1.0] * len(self.restaurants_by_id)
        )

        oid = await self.client_emus[client.id].create_order(rest, client.home)
        self.orders[oid] = {
            "id": oid,
            "client_id": client.id,
            "restaurant_id": rest.id,
            "address": client.home,
            "created_at": self.clock,
            "status": OrderStatus.NEW,
        }
        self.metrics.orders_created += 1

        self.queue.push(self.clock + self.rng.uniform(1.0, 3.0),
                        EventType.RESTAURANT_DECISION, {"order_id": oid})

    async def _on_restaurant_decision(self, oid: int) -> None:
        order = self.orders.get(oid)
        if not order:
            return
        rest = self.restaurants_by_id[order["restaurant_id"]]
        remu = self.rest_emus[rest.id]

        if not remu.decides_accept():
            # Backend doesn't have reject - just leave as NEW (will timeout)
            order["status"] = OrderStatus.NEW
            self.metrics.orders_rejected += 1
            return

        await remu.accept(oid)
        order["status"] = OrderStatus.ACCEPTED
        order["accepted_at"] = self.clock
        rest.active_orders += 1
        self.metrics.orders_accepted += 1

        self.queue.push(self.clock + rest.sample_prep_time(self.rng),
                        EventType.PREP_DONE, {"order_id": oid})

    async def _on_prep_done(self, oid: int) -> None:
        order = self.orders.get(oid)
        if not order:
            return
        rest = self.restaurants_by_id[order["restaurant_id"]]

        await self.rest_emus[rest.id].mark_ready(oid)
        order["status"] = OrderStatus.READY
        order["ready_at"] = self.clock
        rest.active_orders = max(0, rest.active_orders - 1)

        if order.get("accepted_at") is not None:
            self.metrics.prep_times.append(order["ready_at"] - order["accepted_at"])

    async def _on_courier_tick(self, cid: int) -> None:
        courier = self.couriers_by_id[cid]
        cemu = self.cour_emus[cid]

        # 1. движение по маршруту
        if courier.route:
            courier.advance(self.courier_tick, self.rng)
            wp = courier.route[0]
            target = Point(wp["location"]["x"], wp["location"]["y"])

            if courier.position.distance(target) < ARRIVAL_THRESHOLD_M:
                await cemu.complete_waypoint(wp)
                courier.route.pop(0)

                order = self.orders.get(wp["order_id"])
                if order and wp["type"] == "pickup":
                    order["status"] = OrderStatus.PICKED_UP
                    order["picked_up_at"] = self.clock
                    if order.get("ready_at") is not None:
                        self.metrics.ready_to_pickup.append(self.clock - order["ready_at"])
                elif order and wp["type"] == "dropoff":
                    order["status"] = OrderStatus.DELIVERED
                    order["delivered_at"] = self.clock
                    self.metrics.orders_delivered += 1
                    self.metrics.delivery_times.append(self.clock - order["created_at"])

                # маршрут закончился → курьер свободен
                if not courier.route:
                    courier.active_order_id = None
                    courier.status = "IDLE"

        # 2. heartbeat (location update)
        await cemu.heartbeat()

        # 3. опрос доступных заказов, если свободен
        if courier.status == "IDLE" and not courier.route:
            offer = await cemu.poll_offers()
            if offer:
                # курьер «думает» 1–3 сим-секунды
                self.queue.push(self.clock + self.rng.uniform(1.0, 3.0),
                                EventType.OFFER_DECISION, {"courier_id": cid})

        # 4. следующий тик
        if self.clock < self.duration:
            self.queue.push(self.clock + self.courier_tick,
                            EventType.COURIER_TICK, {"courier_id": cid})

    async def _on_offer_decision(self, cid: int) -> None:
        courier = self.couriers_by_id[cid]
        cemu = self.cour_emus[cid]
        offer = cemu.pending_order_id
        if offer is None:
            return
        
        # Get full offer data
        offer_data = {"id": offer, "order_id": offer}
        if cemu.decides_accept(offer_data):
            await cemu.accept_order(offer_data)
            # Build route: pickup at restaurant, dropoff at client
            order = self.orders.get(offer)
            if order:
                rest = self.restaurants_by_id[order["restaurant_id"]]
                courier.route = [
                    {"type": "pickup", "order_id": offer, "location": {"x": rest.position.x, "y": rest.position.y}},
                    {"type": "dropoff", "order_id": offer, "location": {"x": order["address"].x, "y": order["address"].y}},
                ]
                courier.active_order_id = offer
                courier.status = "BUSY"
                cemu.pending_order_id = None
                self.metrics.offer_accepts += 1
                self.metrics.batch_sizes.append(1)
        else:
            await cemu.decline_order(offer_data)
            cemu.pending_order_id = None
            courier.status = "IDLE"
            self.metrics.offer_declines += 1
