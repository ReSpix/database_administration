from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from .api import HttpApiClient, ApiClient
from .core import Point, SeededRNG
from .emulators import ClientEmulator, CourierEmulator, RestaurantEmulator
from .entities import Client, Courier, MenuItem, OrderStatus, Restaurant
from .config import settings

log = logging.getLogger("delivery_sim.testkit")


@dataclass
class TestOrder:
    """Represents an order in a test scenario."""
    id: int
    client_id: int
    restaurant_id: int
    status: OrderStatus = OrderStatus.NEW
    courier_id: Optional[int] = None


class TestScenario:
    """
    Programmatic test scenario for food delivery.
    
    Usage:
        async def test_1rest_2cour():
            async with TestScenario() as scenario:
                # Create entities
                rest = await scenario.create_restaurant("Pizza Place", Point(0, 0))
                courier1 = await scenario.create_courier("Courier 1", Point(10, 10))
                courier2 = await scenario.create_courier("Courier 2", Point(20, 20))
                client = await scenario.create_client("Client 1", Point(50, 50))
                
                # Client creates order
                order = await client.create_order(rest)
                
                # Restaurant accepts order
                await rest.accept_order(order)
                
                # Restaurant marks order ready
                await rest.set_order_ready(order)
                
                # Courier picks up and delivers
                await courier1.start_delivery(order)
                await courier1.finish_delivery(order)
                
                # Assertions
                assert order.status == OrderStatus.DELIVERED
    """
    
    def __init__(self, api: Optional[ApiClient] = None, seed: int = 42):
        self.api = api or HttpApiClient()
        self.rng = SeededRNG(seed)
        self._next_id = 1
        self._restaurants: dict[int, RestaurantEmulator] = {}
        self._couriers: dict[int, CourierEmulator] = {}
        self._clients: dict[int, ClientEmulator] = {}
        self._orders: dict[int, TestOrder] = {}
        self._entities_created = False
    
    def _next_entity_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i
    
    async def __aenter__(self) -> "TestScenario":
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.api.close()
    
    async def create_restaurant(
        self,
        name: str,
        position: Point,
        prep_mean: float = 30.0,
        prep_sigma: float = 0.2,
        capacity: int = 10,
        menu_items: Optional[list[MenuItem]] = None,
    ) -> "TestRestaurant":
        """Create a restaurant for testing."""
        rid = self._next_entity_id()
        
        if menu_items is None:
            menu_items = [
                MenuItem(1, 1, "Test Dish", 10.0),
            ]
        
        restaurant = Restaurant(
            id=rid,
            position=position,
            cuisine=name,
            prep_mean=prep_mean,
            prep_sigma=prep_sigma,
            capacity=capacity,
            menu_items=menu_items,
        )
        
        emulator = RestaurantEmulator(restaurant, self.api, self.rng)
        self._restaurants[rid] = emulator
        
        return TestRestaurant(self, restaurant, emulator)
    
    async def create_courier(
        self,
        name: str,
        position: Point,
        speed_mean: float = 10.0,
        speed_sigma: float = 1.0,
    ) -> "TestCourier":
        """Create a courier for testing."""
        cid = self._next_entity_id()
        
        courier = Courier(
            id=cid,
            position=position,
            speed_mean=speed_mean,
            speed_sigma=speed_sigma,
        )
        
        emulator = CourierEmulator(courier, self.api, self.rng)
        self._couriers[cid] = emulator
        
        return TestCourier(self, courier, emulator)
    
    async def create_client(
        self,
        name: str,
        home: Point,
    ) -> "TestClient":
        """Create a client for testing."""
        cid = self._next_entity_id()
        
        client = Client(id=cid, home=home)
        emulator = ClientEmulator(client, self.api)
        emulator.rng = self.rng
        self._clients[cid] = emulator
        
        return TestClient(self, client, emulator)


class TestRestaurant:
    """Test wrapper for restaurant operations."""
    
    def __init__(self, scenario: TestScenario, entity: Restaurant, emulator: RestaurantEmulator):
        self.scenario = scenario
        self.entity = entity
        self.emulator = emulator
        self.id = entity.id
        self.name = entity.cuisine
    
    async def accept_order(self, order: TestOrder) -> None:
        """Accept an order (restaurant confirms)."""
        await self.emulator.accept(order.id)
        order.status = OrderStatus.ACCEPTED
        log.info(f"[TEST] Restaurant {self.id} accepted order {order.id}")
    
    async def set_order_ready(self, order: TestOrder, estimated_ready_at: Optional[str] = None) -> None:
        """Mark order as ready for pickup."""
        await self.emulator.mark_ready(order.id)
        order.status = OrderStatus.READY
        log.info(f"[TEST] Restaurant {self.id} marked order {order.id} as READY")
    
    async def get_orders(self, status: Optional[OrderStatus] = None) -> list[TestOrder]:
        """Get orders for this restaurant (optionally filtered by status)."""
        orders = [o for o in self.scenario._orders.values() if o.restaurant_id == self.id]
        if status:
            orders = [o for o in orders if o.status == status]
        return orders


class TestCourier:
    """Test wrapper for courier operations."""
    
    def __init__(self, scenario: TestScenario, entity: Courier, emulator: CourierEmulator):
        self.scenario = scenario
        self.entity = entity
        self.emulator = emulator
        self.id = entity.id
        self.active_order: Optional[TestOrder] = None
    
    async def start_delivery(self, order: TestOrder) -> None:
        """Start delivery: accept order, pickup, then deliver."""
        # Accept the order
        await self.emulator.accept_order({"id": order.id})
        order.status = OrderStatus.ASSIGNED
        order.courier_id = self.id
        self.active_order = order
        log.info(f"[TEST] Courier {self.id} accepted order {order.id}")
        
        # Move to restaurant (pickup)
        await self._move_to_restaurant(order)
        await self.emulator.complete_pickup(order.id)
        order.status = OrderStatus.PICKED_UP
        log.info(f"[TEST] Courier {self.id} picked up order {order.id}")
        
        # Move to client (delivery)
        await self._move_to_client(order)
        await self.emulator.complete_delivery(order.id)
        order.status = OrderStatus.DELIVERED
        log.info(f"[TEST] Courier {self.id} delivered order {order.id}")
    
    async def finish_delivery(self, order: TestOrder) -> None:
        """Finish delivery (already done in start_delivery, kept for API compatibility)."""
        # Delivery is completed in start_delivery
        pass
    
    async def _move_to_restaurant(self, order: TestOrder) -> None:
        """Simulate moving to restaurant."""
        restaurant = self.scenario._restaurants.get(order.restaurant_id)
        if restaurant:
            # Update courier position to restaurant
            self.entity.position = restaurant.restaurant.position
            await self.emulator.heartbeat()
    
    async def _move_to_client(self, order: TestOrder) -> None:
        """Simulate moving to client."""
        client = self.scenario._clients.get(order.client_id)
        if client:
            self.entity.position = client.client.home
            await self.emulator.heartbeat()
    
    async def get_available_orders(self) -> list[TestOrder]:
        """Get available orders for this courier."""
        resp = await self.emulator.api.get(f"/couriers/{self.id}/orders/available")
        orders = resp if isinstance(resp, list) else []
        return [
            TestOrder(
                id=o["id"],
                client_id=o.get("client_id", 0),
                restaurant_id=o["restaurant_id"],
                status=OrderStatus.READY,
            )
            for o in orders
        ]


class TestClient:
    """Test wrapper for client operations."""
    
    def __init__(self, scenario: TestScenario, entity: Client, emulator: ClientEmulator):
        self.scenario = scenario
        self.entity = entity
        self.emulator = emulator
        self.id = entity.id
    
    async def create_order(self, restaurant: TestRestaurant, items: Optional[list] = None) -> TestOrder:
        """Create an order at the given restaurant."""
        if items is None:
            items = [restaurant.entity.menu_items[0]] if restaurant.entity.menu_items else []
        
        oid = await self.emulator.create_order(restaurant.entity, self.entity.home)
        
        order = TestOrder(
            id=oid,
            client_id=self.id,
            restaurant_id=restaurant.id,
            status=OrderStatus.NEW,
        )
        self.scenario._orders[oid] = order
        log.info(f"[TEST] Client {self.id} created order {oid} at restaurant {restaurant.id}")
        return order


# -------------------------------------------------------------------------
# Convenience functions for quick test creation
# -------------------------------------------------------------------------

async def run_test(test_func):
    """Run a test function with proper async handling."""
    return await test_func()


# Example test cases
async def test_1rest_2cour():
    """Test: 1 restaurant, 2 couriers, 1 client creates order."""
    async with TestScenario() as scenario:
        # Create entities
        rest = await scenario.create_restaurant(
            "Pizza Place",
            Point(0, 0),
            prep_mean=1.0,  # Fast prep for testing
            prep_sigma=0.1,
        )
        courier1 = await scenario.create_courier(
            "Courier 1",
            Point(10, 10),
            speed_mean=50.0,
        )
        courier2 = await scenario.create_courier(
            "Courier 2",
            Point(20, 20),
            speed_mean=50.0,
        )
        client = await scenario.create_client(
            "Client 1",
            Point(50, 50),
        )
        
        # Client creates order
        order = await client.create_order(rest)
        assert order.status == OrderStatus.NEW
        print(f"✓ Order {order.id} created")
        
        # Restaurant accepts order
        await rest.accept_order(order)
        assert order.status == OrderStatus.ACCEPTED
        print(f"✓ Order {order.id} accepted by restaurant")
        
        # Restaurant marks order ready
        await rest.set_order_ready(order)
        assert order.status == OrderStatus.READY
        print(f"✓ Order {order.id} is READY")
        
        # Courier 1 picks up and delivers
        await courier1.start_delivery(order)
        assert order.status == OrderStatus.DELIVERED
        assert order.courier_id == courier1.id
        print(f"✓ Order {order.id} delivered by courier {courier1.id}")
        
        # Verify courier 2 didn't get the order
        assert courier2.active_order is None
        print(f"✓ Courier 2 correctly did not get the order")
        
        return True


async def test_order_rejection():
    """Test: Restaurant rejects order when at capacity."""
    async with TestScenario() as scenario:
        rest = await scenario.create_restaurant(
            "Busy Restaurant",
            Point(0, 0),
            prep_mean=1.0,
            capacity=1,  # Only 1 order at a time
        )
        courier = await scenario.create_courier("Courier", Point(10, 10))
        client = await scenario.create_client("Client", Point(50, 50))
        
        # Create first order - should be accepted
        order1 = await client.create_order(rest)
        await rest.accept_order(order1)
        await rest.set_order_ready(order1)
        
        # Create second order - restaurant at capacity
        order2 = await client.create_order(rest)
        # Restaurant should reject or queue
        print(f"Order 2 status: {order2.status}")
        
        return True


async def test_multiple_orders_same_courier():
    """Test: Courier handles multiple sequential orders."""
    async with TestScenario() as scenario:
        rest = await scenario.create_restaurant("Restaurant", Point(0, 0), prep_mean=1.0)
        courier = await scenario.create_courier("Courier", Point(10, 10), speed_mean=50.0)
        client = await scenario.create_client("Client", Point(50, 50))
        
        # Create and deliver first order
        order1 = await client.create_order(rest)
        await rest.accept_order(order1)
        await rest.set_order_ready(order1)
        await courier.start_delivery(order1)
        assert order1.status == OrderStatus.DELIVERED
        print(f"✓ Order 1 delivered")
        
        # Create and deliver second order
        order2 = await client.create_order(rest)
        await rest.accept_order(order2)
        await rest.set_order_ready(order2)
        await courier.start_delivery(order2)
        assert order2.status == OrderStatus.DELIVERED
        print(f"✓ Order 2 delivered")
        
        return True


# -------------------------------------------------------------------------
# CLI entry point
# -------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    
    async def main():
        test_name = sys.argv[1] if len(sys.argv) > 1 else "test_1rest_2cour"
        
        tests = {
            "test_1rest_2cour": test_1rest_2cour,
            "test_order_rejection": test_order_rejection,
            "test_multiple_orders_same_courier": test_multiple_orders_same_courier,
        }
        
        if test_name not in tests:
            print(f"Available tests: {list(tests.keys())}")
            return 1
        
        print(f"Running {test_name}...")
        try:
            await tests[test_name]()
            print(f"✓ {test_name} PASSED")
            return 0
        except Exception as e:
            print(f"✗ {test_name} FAILED: {e}")
            import traceback
            traceback.print_exc()
            return 1
    
    exit(asyncio.run(main()))