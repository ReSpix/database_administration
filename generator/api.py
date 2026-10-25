from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

import httpx

from .config import settings

log = logging.getLogger(__name__)


class ApiClient(ABC):
    @abstractmethod
    async def post(self, path: str, payload: dict | None = None) -> dict: ...
    @abstractmethod
    async def get(self, path: str) -> dict: ...
    @abstractmethod
    async def put(self, path: str, payload: dict | None = None) -> dict: ...
    async def close(self) -> None: ...


class HttpApiClient(ApiClient):
    """HTTP-клиент с ретраями на транспортные ошибки и 5xx."""

    RETRYABLE_STATUS = {502, 503, 504}

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        retry_backoff: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.api_base_url).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.api_timeout
        self.max_retries = max_retries if max_retries is not None else settings.api_max_retries
        self.retry_backoff = retry_backoff if retry_backoff is not None else settings.api_retry_backoff
        self._client: httpx.AsyncClient | None = None
        log.info(
            "HttpApiClient → %s (timeout=%.1fs, retries=%d)",
            self.base_url, self.timeout, self.max_retries,
        )

    async def _ensure(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=self.timeout,
            )
        return self._client

    async def _request(self, method: str, path: str, json: dict | None) -> dict:
        client = await self._ensure()
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                r = await client.request(method, path, json=json)
                if r.status_code in self.RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"{r.status_code}", request=r.request, response=r
                    )
                r.raise_for_status()
                return r.json() if r.content else {}
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last_exc = e
                retryable = (
                    isinstance(e, httpx.TransportError)
                    or (isinstance(e, httpx.HTTPStatusError)
                        and e.response.status_code in self.RETRYABLE_STATUS)
                )
                if not retryable or attempt >= self.max_retries:
                    break
                delay = self.retry_backoff * (2 ** attempt)
                log.warning("%s %s failed (%s), retry %d/%d in %.2fs",
                            method, path, e, attempt + 1, self.max_retries, delay)
                await asyncio.sleep(delay)

        assert last_exc is not None
        raise last_exc

    async def post(self, path: str, payload: dict | None = None) -> dict:
        return await self._request("POST", path, payload or {})

    async def get(self, path: str) -> dict:
        return await self._request("GET", path, None)

    async def put(self, path: str, payload: dict | None = None) -> dict:
        return await self._request("PUT", path, payload or {})

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# --- Mock для unit-тестов движка (не используется в main) ---
class MockApiClient(ApiClient):
    """
    Один заказ на курьера. Офферы выдаются «по запросу» IDLE-курьера,
    отказ возвращает заказ в пул — следующий IDLE-курьер его получит.
    """
    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.restaurant_positions: dict[str, dict] = {}
        self.couriers: dict[str, dict] = {}
        self.ready_pool: list[str] = []                # ← очередь готовых заказов
        self.pending_offers: dict[str, dict] = {}      # courier_id → offer
        self._offer_seq = 0
        self.calls: list[tuple] = []

    def register_restaurant(self, rid: str, pos: dict) -> None:
        self.restaurant_positions[rid] = pos

    # ---------------------------------------------------------------- POST
    async def post(self, path: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        self.calls.append(("POST", path, payload))
        p = path.strip("/").split("/")

        # создание заказа
        if p == ["v1", "orders"]:
            oid = f"ord_{len(self.orders) + 1}"
            self.orders[oid] = {"id": oid, **payload, "status": "created"}
            return {"orderId": oid}

        # ресторан
        if len(p) == 5 and p[1] == "restaurant" and p[4] == "accept":
            self.orders[p[3]]["status"] = "accepted"; return {"ok": True}

        if len(p) == 5 and p[1] == "restaurant" and p[4] == "reject":
            self.orders[p[3]]["status"] = "rejected"; return {"ok": True}

        if len(p) == 5 and p[1] == "restaurant" and p[4] == "ready":
            oid = p[3]
            self.orders[oid]["status"] = "ready"
            self.ready_pool.append(oid)
            return {"ok": True}

        # heartbeat
        if len(p) == 4 and p[1] == "couriers" and p[3] == "status":
            cid = p[2]
            prev = self.couriers.get(cid, {}).get("status")
            self.couriers[cid] = dict(payload)
            # если курьер сам объявил себя IDLE (например, после cancel)
            if payload.get("status") == "IDLE" and cid in self.pending_offers:
                self._return_offer_to_pool(cid)
            return {"ok": True}

        # accept / decline оффера
        if len(p) == 6 and p[1] == "couriers" and p[3] == "offers" and p[5] == "accept":
            cid, oid = p[2], p[4]
            self.pending_offers.pop(cid, None)
            self.couriers[cid]["status"] = "BUSY"
            self.orders[oid]["status"] = "assigned"
            self.orders[oid]["courierId"] = cid
            return {"ok": True}

        if len(p) == 6 and p[1] == "couriers" and p[3] == "offers" and p[5] == "decline":
            self._return_offer_to_pool(p[2]); return {"ok": True}

        # complete-pickup / complete-delivery
        if len(p) == 6 and p[1] == "couriers" and p[3] == "tasks":
            cid, oid, action = p[2], p[4], p[5]
            if action == "complete-pickup":
                self.orders[oid]["status"] = "picked_up"
            elif action == "complete-delivery":
                self.orders[oid]["status"] = "delivered"
                self.couriers[cid]["status"] = "IDLE"      # ← освобождаем курьера
            return {"ok": True}

        return {"ok": True}

    # ---------------------------------------------------------------- GET
    async def get(self, path: str) -> dict:
        self.calls.append(("GET", path, None))
        p = path.strip("/").split("/")

        if len(p) == 4 and p[1] == "couriers" and p[3] == "offers":
            cid = p[2]
            # уже есть оффер — возвращаем, не создаём новый
            if cid in self.pending_offers:
                return {"offers": [self.pending_offers[cid]]}
            # только IDLE и есть готовый заказ
            if self.couriers.get(cid, {}).get("status") != "IDLE":
                return {"offers": []}
            if not self.ready_pool:
                return {"offers": []}

            oid = self.ready_pool.pop(0)
            order = self.orders[oid]
            rpos = self.restaurant_positions[order["restaurantId"]]
            self._offer_seq += 1
            offer = {
                "id": f"offer_{self._offer_seq}",
                "orderId": oid,
                "route": [
                    {"type": "pickup",  "orderId": oid, "location": rpos},
                    {"type": "dropoff", "orderId": oid, "location": order["address"]},
                ],
            }
            self.pending_offers[cid] = offer
            self.couriers[cid]["status"] = "RESERVED"
            return {"offers": [offer]}

        return {}

    # ---------------------------------------------------------------- helpers
    def _return_offer_to_pool(self, cid: str) -> None:
        offer = self.pending_offers.pop(cid, None)
        if offer and self.orders[offer["orderId"]]["status"] == "ready":
            self.ready_pool.insert(0, offer["orderId"])      # в начало очереди
        if cid in self.couriers and self.couriers[cid].get("status") == "RESERVED":
            self.couriers[cid]["status"] = "IDLE"