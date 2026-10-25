from __future__ import annotations

import asyncio
import logging

from .api import ApiClient
from .core import Event, EventQueue, EventType, Point, SeededRNG
from .emulators import ClientEmulator, CourierEmulator, RestaurantEmulator
from .entities import Client, Courier, MenuItem, OrderStatus, Restaurant
from .metrics import Metrics

ARRIVAL_THRESHOLD_M = 5.0

log = logging.getLogger("delivery_sim.engine")


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
        verbose: bool = False,
    ) -> None:
        self.api = api
        self.rng = rng
        self.duration = duration
        self.order_rate = order_rate
        self.courier_tick = courier_tick
        self.verbose = verbose

        self.clients_by_id     = {c.id: c for c in clients}
        self.restaurants_by_id = {r.id: r for r in restaurants}
        self.couriers_by_id    = {c.id: c for c in couriers}

        self.client_emus = {c.id: ClientEmulator(c, api) for c in clients}
        for emu in self.client_emus.values():
            emu.rng = rng
        self.rest_emus   = {r.id: RestaurantEmulator(r, api, rng) for r in restaurants}
        self.cour_emus   = {c.id: CourierEmulator(c, api, rng) for c in couriers}

        self.orders: dict[int, dict] = {}
        self.clock = 0.0
        self.queue = EventQueue()
        self.metrics = Metrics()

    def _log(self, event_type: str, msg: str) -> None:
        if self.verbose:
            log.info(f"[t={self.clock:.1f}] [{event_type}] {msg}")
    
    def _debug(self, event_type: str, msg: str) -> None:
        if self.verbose:
            log.debug(f"[t={self.clock:.1f}] [{event_type}] {msg}")

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
                log.error(f"[t={self.clock:.1f}] error handling {ev.type.name}: {e}")

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
        self._log("ORDER_CREATED", f"order_id={oid} client_id={client.id} restaurant_id={rest.id}")

        self.queue.push(self.clock + self.rng.uniform(1.0, 3.0),
                        EventType.RESTAURANT_DECISION, {"order_id": oid})

    async def _on_restaurant_decision(self, oid: int) -> None:
        order = self.orders.get(oid)
        if not order:
            return
        rest = self.restaurants_by_id[order["restaurant_id"]]
        remu = self.rest_emus[rest.id]

        if not remu.decides_accept():
            order["status"] = OrderStatus.NEW
            self.metrics.orders_rejected += 1
            self._log("ORDER_REJECTED", f"order_id={oid} restaurant_id={rest.id} reason=busy")
            return

        await remu.accept(oid)
        order["status"] = OrderStatus.ACCEPTED
        order["accepted_at"] = self.clock
        rest.active_orders += 1
        self.metrics.orders_accepted += 1
        self._log("ORDER_ACCEPTED", f"order_id={oid} restaurant_id={rest.id}")

        prep_time = rest.sample_prep_time(self.rng)
        self.queue.push(self.clock + prep_time,
                        EventType.PREP_DONE, {"order_id": oid})
        self._log("COOKING_STARTED", f"order_id={oid} prep_time={prep_time:.0f}s ready_at={self.clock + prep_time:.1f}")

    async def _on_prep_done(self, oid: int) -> None:
        order = self.orders.get(oid)
        if not order:
            return
        rest = self.restaurants_by_id[order["restaurant_id"]]

        await self.rest_emus[rest.id].mark_ready(oid)
        order["status"] = OrderStatus.READY
        order["ready_at"] = self.clock
        rest.active_orders = max(0, rest.active_orders - 1)
        self._log("ORDER_READY", f"order_id={oid} restaurant_id={rest.id}")

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
                        wait_time = self.clock - order["ready_at"]
                        self.metrics.ready_to_pickup.append(wait_time)
                        self._log("ORDER_PICKED_UP", f"order_id={order['id']} courier_id={cid} wait_time={wait_time:.0f}s")
                elif order and wp["type"] == "dropoff":
                    order["status"] = OrderStatus.DELIVERED
                    order["delivered_at"] = self.clock
                    self.metrics.orders_delivered += 1
                    delivery_time = self.clock - order["created_at"]
                    self.metrics.delivery_times.append(delivery_time)
                    self._log("ORDER_DELIVERED", f"order_id={order['id']} courier_id={cid} total_time={delivery_time:.0f}s")

                # маршрут закончился -> курьер свободен
                if not courier.route:
                    courier.active_order_id = None
                    courier.status = "IDLE"
                    self._log("COURIER_IDLE", f"courier_id={cid}")

        # 2. heartbeat (location update)
        await cemu.heartbeat()

        # 3. опрос доступных заказов, если свободен
        if courier.status == "IDLE" and not courier.route:
            offer = await cemu.poll_offers()
            if offer:
                offer_id = offer.get('id', offer.get('order_id', 'unknown'))
                self._log("OFFER_RECEIVED", f"courier_id={cid} offer_id={offer_id}")
                # Only schedule decision if not already pending for this courier
                if not getattr(courier, 'decision_pending', False):
                    courier.decision_pending = True
                    self.queue.push(self.clock + self.rng.uniform(1.0, 3.0),
                                    EventType.OFFER_DECISION, {"courier_id": cid})

        # 4. следующий тик
        if self.clock < self.duration:
            self.queue.push(self.clock + self.courier_tick,
                            EventType.COURIER_TICK, {"courier_id": cid})

    async def _on_offer_decision(self, cid: int) -> None:
        courier = self.couriers_by_id[cid]
        cemu = self.cour_emus[cid]
        # Reset decision pending flag
        courier.decision_pending = False
        self._debug("OFFER_DECISION", f"courier_id={cid} status={courier.status} route={courier.route} pending_offer={cemu.pending_offer}")
        offer = cemu.pending_offer
        if offer is None:
            self._debug("OFFER_NONE", f"courier_id={cid} no pending offer")
            return
        
        offer_id = offer.get('id', offer.get('order_id', 'unknown'))
        self._debug("DECIDES_ACCEPT", f"courier_id={cid} offer_id={offer_id}")
        if cemu.decides_accept(offer):
            self._debug("ACCEPTING", f"courier_id={cid} offer_id={offer_id}")
            # Check if we know about this order
            order = self.orders.get(offer_id)
            if order is None:
                # Unknown order - decline it
                self._log("OFFER_UNKNOWN", f"courier_id={cid} offer_id={offer_id} not in local orders, declining")
                await cemu.decline_order(offer)
                cemu.pending_offer = None
                courier.status = "IDLE"
                self.metrics.offer_declines += 1
                return
            
            self._debug("ACCEPTING", f"courier_id={cid} offer_id={offer_id}")
            await cemu.accept_order(offer)
            order = self.orders.get(offer_id)
            if order:
                rest = self.restaurants_by_id[order["restaurant_id"]]
                courier.route = [
                    {"type": "pickup", "order_id": offer_id, "location": {"x": rest.position.x, "y": rest.position.y}},
                    {"type": "dropoff", "order_id": offer_id, "location": {"x": order["address"].x, "y": order["address"].y}},
                ]
                courier.active_order_id = offer_id
                courier.status = "BUSY"
                cemu.pending_offer = None
                self.metrics.offer_accepts += 1
                self.metrics.batch_sizes.append(1)
                self._log("OFFER_ACCEPTED", f"courier_id={cid} order_id={offer_id} restaurant_id={rest.id}")
        else:
            await cemu.decline_order(offer)
            cemu.pending_offer = None
            courier.status = "IDLE"
            self.metrics.offer_declines += 1
            self._log("OFFER_DECLINED", f"courier_id={cid} order_id={offer_id}")