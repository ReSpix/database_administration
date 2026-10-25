from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .core import Point
from .entities import Client, Courier, MenuItem, Restaurant


@dataclass
class ScenarioConfig:
    """Configuration for a simulation scenario."""
    name: str
    description: str
    duration: float = 10.0
    order_rate: float = 0.5          # заказов в секунду
    courier_tick: float = 1.0        # тик курьера в секундах
    seed: int = 42
    
    # Custom entities (if None, will be generated from counts below)
    clients: list[Client] = field(default_factory=list)
    restaurants: list[Restaurant] = field(default_factory=list)
    couriers: list[Courier] = field(default_factory=list)
    
    # Counts for auto-generation (used if lists above are empty)
    num_clients: int = 3
    num_restaurants: int = 2
    num_couriers: int = 1
    
    # Area bounds for random placement (meters from origin)
    area_size: float = 1000.0
    
    # Restaurant params
    prep_mean_range: tuple[float, float] = (120.0, 300.0)  # сек
    prep_sigma: float = 0.3
    capacity: int = 10
    
    # Courier params
    speed_mean: float = 5.0
    speed_sigma: float = 0.5
    
    # Client params
    cancel_prob: float = 0.0
    
    # Logging
    verbose: bool = True


# Predefined scenarios
SCENARIOS = {
    "quick_demo": ScenarioConfig(
        name="quick_demo",
        description="Quick demo: 3 clients, 2 restaurants, 1 courier, 10 sec",
        duration=10.0,
        order_rate=0.5,
        courier_tick=1.0,
        num_clients=3,
        num_restaurants=2,
        num_couriers=1,
        area_size=500.0,
        verbose=True,
    ),
    
    "single_courier_two_restaurants": ScenarioConfig(
        name="single_courier_two_restaurants",
        description="1 courier, 2 restaurants with different prep times, 3 clients, ~3 orders in 10 sec",
        duration=15.0,
        order_rate=0.3,
        courier_tick=1.0,
        seed=123,
        num_clients=3,
        num_restaurants=2,
        num_couriers=1,
        area_size=150.0,
        prep_mean_range=(1.0, 3.0),
        prep_sigma=0.1,
        verbose=True,
        speed_mean=50.0,
        speed_sigma=5.0,
    ),
    
    "stress_test": ScenarioConfig(
        name="stress_test",
        description="High load: 20 clients, 5 restaurants, 10 couriers",
        duration=30.0,
        order_rate=2.0,
        courier_tick=0.5,
        num_clients=20,
        num_restaurants=5,
        num_couriers=10,
        area_size=2000.0,
        verbose=False,
    ),
    
    "slow_observation": ScenarioConfig(
        name="slow_observation",
        description="Very slow for human observation: 2 clients, 1 restaurant, 1 courier",
        duration=20.0,
        order_rate=0.1,          # 1 заказ в 10 сек
        courier_tick=2.0,
        num_clients=2,
        num_restaurants=1,
        num_couriers=1,
        area_size=300.0,
        prep_mean_range=(180.0, 180.0),
        verbose=True,
    ),
}


def create_scenario_entities(config: ScenarioConfig, rng) -> tuple[list[Client], list[Restaurant], list[Courier]]:
    """Create entities based on scenario config."""
    
    # Use custom entities if provided, otherwise generate
    if config.clients:
        clients = config.clients
    else:
        clients = [
            Client(i, home=Point(
                rng.uniform(-config.area_size, config.area_size),
                rng.uniform(-config.area_size, config.area_size)
            ), cancel_prob=config.cancel_prob)
            for i in range(1, config.num_clients + 1)
        ]
    
    if config.restaurants:
        restaurants = config.restaurants
    else:
        restaurants = []
        for i in range(1, config.num_restaurants + 1):
            prep_mean = rng.uniform(*config.prep_mean_range)
            menu_items = [
                MenuItem(i * 10 + j, i * 10 + j, f"Dish {i}-{j}", 10.0 + j * 2.0)
                for j in range(1, 4)
            ]
            restaurants.append(Restaurant(
                i,
                Point(
                    rng.uniform(-config.area_size, config.area_size),
                    rng.uniform(-config.area_size, config.area_size)
                ),
                f"cuisine_{i}",
                prep_mean=prep_mean,
                prep_sigma=config.prep_sigma,
                capacity=config.capacity,
                menu_items=menu_items,
            ))
    
    if config.couriers:
        couriers = config.couriers
    else:
        couriers = [
            Courier(i, Point(
                rng.uniform(-config.area_size, config.area_size),
                rng.uniform(-config.area_size, config.area_size)
            ), speed_mean=config.speed_mean, speed_sigma=config.speed_sigma)
            for i in range(1, config.num_couriers + 1)
        ]
    
    return clients, restaurants, couriers


def get_scenario(name: str) -> Optional[ScenarioConfig]:
    """Get scenario by name."""
    return SCENARIOS.get(name)


def list_scenarios() -> list[str]:
    """List available scenario names."""
    return list(SCENARIOS.keys())
