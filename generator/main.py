from __future__ import annotations

import asyncio
import json
import logging
import sys

from .api import HttpApiClient
from .config import settings
from .core import Point, SeededRNG
from .engine import SimulationEngine
from .entities import Client, Courier, Restaurant

log = logging.getLogger("delivery_sim")


def _build_world():
    clients = [Client(f"c{i}", home=Point(500 + i * 100, 500)) for i in range(20)]
    restaurants = [
        Restaurant("r1", Point(1000, 2000), "pizza", prep_mean=300, prep_sigma=0.3, capacity=15),
        Restaurant("r2", Point(3000, 5000), "steak", prep_mean=480, prep_sigma=0.4, capacity=8),
        Restaurant("r3", Point(6000, 3000), "sushi", prep_mean=360, prep_sigma=0.3, capacity=10),
    ]
    couriers = [
        Courier(f"k{i}", Point(2000 + i * 200, 3000), speed_mean=6.0, speed_sigma=1.0)
        for i in range(15)
    ]
    return clients, restaurants, couriers


async def _preflight(api: HttpApiClient) -> bool:
    """Проверяем, что бэкенд жив и отвечает 2xx на health-эндпоинт."""
    if not settings.sim_preflight:
        log.info("Preflight skipped (SIM_PREFLIGHT=false)")
        return True
    try:
        client = await api._ensure()
        r = await client.get(settings.api_health_path)
        ok = 200 <= r.status_code < 300
        log.info("Preflight %s%s → %d %s",
                 api.base_url, settings.api_health_path, r.status_code,
                 "OK" if ok else "FAIL")
        return ok
    except Exception as e:                            # noqa: BLE001
        log.error("Preflight failed: %s", e)
        return False


async def run_simulation() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    rng = SeededRNG(seed=settings.sim_seed)
    clients, restaurants, couriers = _build_world()

    api = HttpApiClient()                             # host/port из .env
    exit_code = 0
    try:
        if not await _preflight(api):
            log.error("Backend is not reachable at %s. Aborting.",
                      settings.api_base_url)
            return 2

        engine = SimulationEngine(
            api=api,
            clients=clients,
            restaurants=restaurants,
            couriers=couriers,
            rng=rng,
            duration=settings.sim_duration,
            order_rate=settings.sim_order_rate,
            courier_tick=settings.sim_courier_tick,
        )

        log.info("Simulation start: duration=%.0fs, rate=%.2f/s, couriers=%d",
                 settings.sim_duration, settings.sim_order_rate, len(couriers))
        metrics = await engine.run()
        print(json.dumps(metrics.summary(), indent=2, ensure_ascii=False))

    except KeyboardInterrupt:
        log.info("Interrupted by user, shutting down…")
        exit_code = 130
    except Exception as e:                            # noqa: BLE001
        log.exception("Simulation crashed: %s", e)
        exit_code = 1
    finally:
        await api.close()

    return exit_code


def main() -> None:
    sys.exit(asyncio.run(run_simulation()))


if __name__ == "__main__":
    main()