from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path

try:
    import asyncpg
except ImportError:
    asyncpg = None

from .api import HttpApiClient
from .config import settings
from .core import Point, SeededRNG
from .engine import SimulationEngine
from .entities import Client, Courier, MenuItem, Restaurant
from .scenarios import ScenarioConfig, create_scenario_entities, get_scenario, list_scenarios

log = logging.getLogger("delivery_sim")

# Log file path in project directory
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "delivery_sim.log"
LOG_TO_CONSOLE = os.getenv("SIM_LOG_CONSOLE", "false").lower() == "true"


def _build_world():
    # Create clients with integer IDs
    # Use meter-based coordinates (origin at Moscow center)
    clients = [
        Client(i, home=Point(500 + i * 100, 500))
        for i in range(1, 21)
    ]

    # Create menu items for restaurants
    pizza_items = [
        MenuItem(1, 1, "Margherita", 12.50),
        MenuItem(2, 2, "Pepperoni", 14.00),
        MenuItem(3, 3, "Vegetarian", 13.00),
        MenuItem(4, 4, "Four Cheese", 15.00),
    ]
    
    steak_items = [
        MenuItem(5, 5, "Ribeye Steak", 25.00),
        MenuItem(6, 6, "Filet Mignon", 30.00),
        MenuItem(7, 7, "Burger", 18.00),
    ]
    
    sushi_items = [
        MenuItem(8, 8, "Salmon Roll", 10.00),
        MenuItem(9, 9, "Tuna Sashimi", 16.00),
        MenuItem(10, 10, "California Roll", 12.00),
        MenuItem(11, 11, "Eel Roll", 14.00),
    ]

    # Restaurants with meter-based positions (relative to Moscow center)
    # Pizza: ~0m, 0m (Moscow center)
    # Steak: ~300m east, 500m north
    # Sushi: ~800m east, 1000m north
    restaurants = [
        Restaurant(1, Point(0, 0), "pizza", prep_mean=300, prep_sigma=0.3, capacity=15, menu_items=pizza_items),
        Restaurant(2, Point(300, 500), "steak", prep_mean=480, prep_sigma=0.4, capacity=8, menu_items=steak_items),
        Restaurant(3, Point(800, 1000), "sushi", prep_mean=360, prep_sigma=0.3, capacity=10, menu_items=sushi_items),
    ]

    # Couriers starting near restaurants (in meters)
    couriers = [
        Courier(i, Point(100 + i * 50, 100 + i * 50), speed_mean=6.0, speed_sigma=1.0)
        for i in range(1, 16)
    ]
    return clients, restaurants, couriers


async def _load_entities_from_backend(
    api: HttpApiClient,
    prep_mean_range: tuple[float, float] = (20.0, 40.0),
    prep_sigma: float = 0.3,
    speed_mean: float = 50.0,
    speed_sigma: float = 5.0,
    area_size: float = 150.0,
) -> tuple[list[Client], list[Restaurant], list[Courier]]:
    """Load entities from backend database via API."""
    # Load clients
    # For now, we'll create entities that match the database seed data
    # In a real scenario, you'd call API endpoints to get this data
    
    # Clients (1-20)
    clients = [Client(i, home=Point(500 + i * 100, 500)) for i in range(1, 21)]
    
    # Menu items matching database
    pizza_items = [
        MenuItem(1, 1, "Margherita", 12.50),
        MenuItem(2, 2, "Pepperoni", 14.00),
        MenuItem(3, 3, "Vegetarian", 13.00),
        MenuItem(4, 4, "Four Cheese", 15.00),
    ]
    steak_items = [
        MenuItem(5, 5, "Ribeye Steak", 25.00),
        MenuItem(6, 6, "Filet Mignon", 30.00),
        MenuItem(7, 7, "Burger", 18.00),
    ]
    sushi_items = [
        MenuItem(8, 8, "Salmon Roll", 10.00),
        MenuItem(9, 9, "Tuna Sashimi", 16.00),
        MenuItem(10, 10, "California Roll", 12.00),
        MenuItem(11, 11, "Eel Roll", 14.00),
    ]
    
    import random
    rng = random.Random(123)  # Fixed seed for reproducible positions
    
    def sample_prep_mean() -> float:
        return rng.uniform(*prep_mean_range)
    
    # Restaurants with correct IDs matching database (1, 2, 3)
    restaurants = [
        Restaurant(1, Point(0, 0), "pizza", 
                  prep_mean=sample_prep_mean(), prep_sigma=prep_sigma, capacity=15, menu_items=pizza_items),
        Restaurant(2, Point(150, 0), "steak", 
                  prep_mean=sample_prep_mean(), prep_sigma=prep_sigma, capacity=8, menu_items=steak_items),
        Restaurant(3, Point(0, 150), "sushi", 
                  prep_mean=sample_prep_mean(), prep_sigma=prep_sigma, capacity=10, menu_items=sushi_items),
    ]
    
    # Couriers starting near restaurants
    couriers = [
        Courier(i, Point(rng.uniform(-area_size/2, area_size/2), rng.uniform(-area_size/2, area_size/2)), 
                speed_mean=speed_mean, speed_sigma=speed_sigma)
        for i in range(1, 16)
    ]
    
    # Clients
    clients = [
        Client(i, home=Point(rng.uniform(-area_size, area_size), rng.uniform(-area_size, area_size)))
        for i in range(1, 21)
    ]
    
    return clients, restaurants, couriers


async def _select_scenario(api: HttpApiClient) -> tuple[ScenarioConfig, list[Client], list[Restaurant], list[Courier]]:
    """Select and build scenario based on settings."""
    scenario_name = os.getenv("SIM_SCENARIO")
    
    if settings.sim_quick:
        # Quick mode - load from backend to match database
        log.info("QUICK MODE: 10 second simulation with verbose logging")
        scenario = ScenarioConfig(
            name="quick",
            description="Quick test mode",
            duration=settings.sim_quick_duration,
            order_rate=settings.sim_quick_order_rate,
            courier_tick=settings.sim_quick_courier_tick,
            seed=settings.sim_seed,
            verbose=True,
        )
        # Load entities from backend for quick mode with fast prep times
        clients, restaurants, couriers = await _load_entities_from_backend(
            api,
            prep_mean_range=(1.0, 3.0),
            prep_sigma=0.1,
            speed_mean=50.0,
            speed_sigma=5.0,
            area_size=150.0,
        )
    elif scenario_name and (scenario := get_scenario(scenario_name)):
        log.info(f"Using scenario: {scenario.name} - {scenario.description}")
        # Load entities from backend to match database
        clients, restaurants, couriers = await _load_entities_from_backend(
            api,
            prep_mean_range=scenario.prep_mean_range,
            prep_sigma=scenario.prep_sigma,
            speed_mean=scenario.speed_mean,
            speed_sigma=scenario.speed_sigma,
            area_size=scenario.area_size,
        )
        # Override counts from scenario
        if scenario.num_couriers > 0:
            couriers = couriers[:scenario.num_couriers]
        if scenario.num_restaurants > 0:
            restaurants = restaurants[:scenario.num_restaurants]
        if scenario.num_clients > 0:
            clients = clients[:scenario.num_clients]
    else:
        # Default: generate from world builder
        scenario = ScenarioConfig(
            name="default",
            description="Default world",
            duration=settings.effective_duration,
            order_rate=settings.effective_order_rate,
            courier_tick=settings.effective_courier_tick,
            seed=settings.sim_seed,
            verbose=False,
        )
        clients, restaurants, couriers = _build_world()
    
    log.info(f"Scenario: {scenario.name} | Duration: {scenario.duration}s | "
             f"Order rate: {scenario.order_rate}/s | Couriers: {len(couriers)} | "
             f"Restaurants: {len(restaurants)} | Clients: {len(clients)}")
    
    return scenario, clients, restaurants, couriers


async def _clean_database(api: HttpApiClient) -> None:
    """Clean up the database before simulation by truncating order tables."""
    # Try direct database connection first
    if asyncpg is not None:
        try:
            # Parse database URL from settings
            db_url = settings.database_url
            if db_url:
                conn = await asyncpg.connect(db_url)
                try:
                    # Truncate order-related tables
                    await conn.execute("""
                        TRUNCATE TABLE order_assignments, order_items, orders RESTART IDENTITY CASCADE;
                    """)
                    log.info("Database cleaned via direct connection (truncated order tables)")
                    return
                finally:
                    await conn.close()
        except Exception as e:
            log.warning(f"Direct database cleanup failed: {e}, falling back to API")
    
    # Fallback: try API cleanup (may not work if no cancel endpoint)
    try:
        orders_resp = await api.get("/orders")
        orders = orders_resp if isinstance(orders_resp, list) else []
        for order in orders:
            oid = order.get("id")
            if oid:
                try:
                    await api.post(f"/orders/{oid}/cancel", {})
                except Exception:
                    pass
        log.info("Database cleaned via API (cancelled orders)")
    except Exception as e:
        log.warning(f"API cleanup failed: {e}")

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
    # Configure logging level
    log_level = logging.DEBUG if settings.sim_quick else logging.INFO
    
    # Silence httpx and other noisy loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    
    # Create formatters
    file_formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    console_formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    
    # Get root logger and clear existing handlers
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # File handler (always on, with flush for real-time)
    log_path = Path(LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, mode='a', encoding='utf-8')
    file_handler.setLevel(log_level)
    file_handler.setFormatter(file_formatter)
    # Flush after each write for real-time viewing
    file_handler.flush = lambda: file_handler.stream.flush()
    root_logger.addHandler(file_handler)
    
    # Optional console handler
    if LOG_TO_CONSOLE:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(console_formatter)
        root_logger.addHandler(console_handler)
    
    log.info(f"Logging to file: {LOG_FILE}")

    api = HttpApiClient()                             # host/port из .env
    exit_code = 0
    try:
        if not await _preflight(api):
            log.error("Backend is not reachable at %s. Aborting.",
                      settings.api_base_url)
            return 2

        # Clean database before simulation
        log.info("Cleaning database before simulation...")
        await _clean_database(api)

        scenario, clients, restaurants, couriers = await _select_scenario(api)
        rng = SeededRNG(seed=scenario.seed)

        engine = SimulationEngine(
            api=api,
            clients=clients,
            restaurants=restaurants,
            couriers=couriers,
            rng=rng,
            duration=scenario.duration,
            order_rate=scenario.order_rate,
            courier_tick=scenario.courier_tick,
            verbose=scenario.verbose,
        )

        log.info("Simulation start: duration=%.0fs, rate=%.2f/s, couriers=%d",
                 scenario.duration, scenario.order_rate, len(couriers))
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
    # Print available scenarios if requested
    import os
    if os.getenv("SIM_LIST_SCENARIOS"):
        print("Available scenarios:")
        for name in list_scenarios():
            sc = get_scenario(name)
            print(f"  {name}: {sc.description}")
        return
    
    sys.exit(asyncio.run(run_simulation()))


if __name__ == "__main__":
    main()