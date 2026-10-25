from __future__ import annotations

import asyncio

from .api import ApiClient
from .core import Event, EventQueue, EventType, Point, SeededRNG
from .emulators import ClientEmulator, CourierEmulator, RestaurantEmulator
from .entities import Client, Courier, Order, OrderStatus, Restaurant
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
        self.rest_emus   = {r.id: RestaurantEmulator(r, api, rng) for r in restaurants}
        self.cour_emus   = {c.id: CourierEmulator(c, api, rng) for c in couriers}

        self.orders: dict[str, Order] = {}
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
        self.orders[oid] = Order(
            id=oid,
            client_id=client.id,
            restaurant_id=rest.id,
            address=client.home,
            created_at=self.clock,
        )
        self.metrics.orders_created += 1

        self.queue.push(self.clock + self.rng.uniform(1.0, 3.0),
                        EventType.RESTAURANT_DECISION, {"order_id": oid})

    async def _on_restaurant_decision(self, oid: str) -> None:
        order = self.orders.get(oid)
        if not order:
            return
        rest = self.restaurants_by_id[order.restaurant_id]
        remu = self.rest_emus[rest.id]

        if not remu.decides_accept():
            await remu.reject(oid)
            order.status = OrderStatus.REJECTED
            self.metrics.orders_rejected += 1
            return

        await remu.accept(oid)
        order.status = OrderStatus.ACCEPTED
        order.accepted_at = self.clock
        rest.active_orders += 1
        self.metrics.orders_accepted += 1

        self.queue.push(self.clock + rest.sample_prep_time(self.rng),
                        EventType.PREP_DONE, {"order_id": oid})

    async def _on_prep_done(self, oid: str) -> None:
        order = self.orders.get(oid)
        if not order:
            return
        rest = self.restaurants_by_id[order.restaurant_id]

        await self.rest_emus[rest.id].mark_ready(oid)
        order.status = OrderStatus.READY
        order.ready_at = self.clock
        rest.active_orders = max(0, rest.active_orders - 1)

        if order.accepted_at is not None:
            self.metrics.prep_times.append(order.ready_at - order.accepted_at)

    async def _on_courier_tick(self, cid: str) -> None:
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

                order = self.orders.get(wp["orderId"])
                if order and wp["type"] == "pickup":
                    order.status = OrderStatus.PICKED_UP
                    order.picked_up_at = self.clock
                    if order.ready_at is not None:
                        self.metrics.ready_to_pickup.append(self.clock - order.ready_at)
                elif order and wp["type"] == "dropoff":
                    order.status = OrderStatus.DELIVERED
                    order.delivered_at = self.clock
                    self.metrics.orders_delivered += 1
                    self.metrics.delivery_times.append(self.clock - order.created_at)

                if wp["orderId"] in courier.active_batch:
                    courier.active_batch.remove(wp["orderId"])
                if not courier.active_batch and not courier.route:
                    courier.status = "IDLE"

        # 2. heartbeat (обновляет позицию в диспетчере)
        await cemu.heartbeat()

        # 3. опрос офферов, если свободен
        if courier.status == "IDLE" and not courier.route:
            offer = await cemu.poll_offers()
            if offer:
                self.queue.push(self.clock + 1.0,
                                EventType.OFFER_DECISION, {"courier_id": cid})

        # 4. следующий тик
        if self.clock < self.duration:
            self.queue.push(self.clock + self.courier_tick,
                            EventType.COURIER_TICK, {"courier_id": cid})

    async def _on_offer_decision(self, cid: str) -> None:
        courier = self.couriers_by_id[cid]
        cemu = self.cour_emus[cid]
        offer = cemu.pending_offer
        if not offer:
            return

        if cemu.decides_accept(offer):
            await cemu.accept_offer(offer)
            courier.route = list(offer["route"])
            courier.active_batch = [offer["orderId"]]
            courier.status = "BUSY"
            cemu.pending_offer = None
            self.metrics.offer_accepts += 1
            self.metrics.batch_sizes.append(len(courier.active_batch))
        else:
            await cemu.decline_offer(offer)
            cemu.pending_offer = None
            self.metrics.offer_declines += 1