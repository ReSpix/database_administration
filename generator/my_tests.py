from generator.testkit import TestScenario, TestOrder, TestCourier, TestRestaurant, TestClient
from generator.core import Point
from generator.entities import OrderStatus

async def test_my_custom_scenario():
    """Custom test: 2 couriers, 1 restaurant, 2 clients."""
    async with TestScenario() as scenario:
        # Create entities
        rest = await scenario.create_restaurant(
            "My Restaurant",
            Point(0, 0),
            prep_mean=1.0,      # Fast prep for testing
            prep_sigma=0.1,
        )
        courier1 = await scenario.create_courier("Courier 1", Point(10, 10), speed_mean=50.0)
        courier2 = await scenario.create_courier("Courier 2", Point(20, 20), speed_mean=50.0)
        client1 = await scenario.create_client("Client 1", Point(50, 50))
        client2 = await scenario.create_client("Client 2", Point(60, 60))
        
        # Client 1 creates order
        order1 = await client1.create_order(rest)
        assert order1.status == OrderStatus.NEW
        
        # Restaurant accepts
        await rest.accept_order(order1)
        
        # Restaurant marks ready
        await rest.set_order_ready(order1)
        
        # Courier 1 delivers
        await courier1.start_delivery(order1)
        assert order1.status == OrderStatus.DELIVERED
        
        # Client 2 creates order
        order2 = await client2.create_order(rest)
        await rest.accept_order(order2)
        await rest.set_order_ready(order2)
        
        # Courier 2 delivers second order
        await courier2.start_delivery(order2)
        assert order2.status == OrderStatus.DELIVERED
        
        print("✓ Custom test passed!")

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_my_custom_scenario())