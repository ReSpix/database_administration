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

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# --- Mock для unit-тестов движка (не используется в main) ---
class MockApiClient(ApiClient):
    """Оставлен только для тестов движка. В main не подключается."""
    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.restaurant_positions: dict[str, dict] = {}
        self.couriers: dict[str, dict] = {}
        self.offers: dict[str, list[dict]] = {}
        self._offer_seq = 0

    def register_restaurant(self, rid: str, pos: dict) -> None:
        self.restaurant_positions[rid] = pos

    async def post(self, path: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        p = path.strip("/").split("/")
        if p == ["v1", "orders"]:
            oid = f"ord_{len(self.orders) + 1}"
            self.orders[oid] = {"id": oid, **payload}
            return {"orderId": oid}
        if len(p) == 5 and p[1] == "restaurant" and p[4] == "accept":
            self.orders[p[3]]["status"] = "accepted"; return {"ok": True}
        if len(p) == 5 and p[1] == "restaurant" and p[4] == "reject":
            self.orders[p[3]]["status"] = "rejected"; return {"ok": True}
        if len(p) == 5 and p[1] == "restaurant" and p[4] == "ready":
            self.orders[p[3]]["status"] = "ready"
            self._form_offer(p[3]); return {"ok": True}
        if len(p) == 4 and p[1] == "couriers" and p[3] == "status":
            self.couriers[p[2]] = dict(payload); return {"ok": True}
        if len(p) == 6 and p[1] == "couriers" and p[3] == "offers":
            self.offers[p[2]] = [
                o for o in self.offers.get(p[2], []) if o["id"] != p[4]
            ]
            return {"ok": True}
        if len(p) == 6 and p[1] == "couriers" and p[3] == "tasks":
            if p[5] == "complete-pickup":
                self.orders[p[4]]["status"] = "picked_up"
            elif p[5] == "complete-delivery":
                self.orders[p[4]]["status"] = "delivered"
            return {"ok": True}
        return {"ok": True}

    async def get(self, path: str) -> dict:
        p = path.strip("/").split("/")
        if len(p) == 4 and p[1] == "couriers" and p[3] == "offers":
            offers = self.offers.get(p[2], [])
            self.offers[p[2]] = []
            return {"offers": offers}
        return {}

    def _form_offer(self, order_id: str) -> None:
        order = self.orders.get(order_id)
        if not order:
            return
        idle = [cid for cid, c in self.couriers.items() if c.get("status") == "IDLE"]
        if not idle:
            return
        rpos = self.restaurant_positions[order["restaurantId"]]
        best = min(idle, key=lambda cid: (
            (self.couriers[cid]["position"]["x"] - rpos["x"]) ** 2
            + (self.couriers[cid]["position"]["y"] - rpos["y"]) ** 2
        ))
        self._offer_seq += 1
        self.offers.setdefault(best, []).append({
            "id": f"offer_{self._offer_seq}",
            "orderId": order_id,
            "route": [
                {"type": "pickup",  "orderId": order_id, "location": rpos},
                {"type": "dropoff", "orderId": order_id, "location": order["address"]},
            ],
        })
        self.couriers[best]["status"] = "RESERVED"

    async def close(self) -> None:
        pass