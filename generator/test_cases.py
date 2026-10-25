from .courer_manager import assign_orders
from .database import get_session

# вот таких нужно 10-15 случаев:


def one_order_one_courier():
    db_session = get_session()

    # создает одного курьера и один заказ
    order = ...
    courier = ...

    # пытается назначить
    assign_orders(db_session, order, [courier])

    # проверяет что назначение прошло
    assignment = ... # Select order_assignment where order_id == order.id
    assert assignment.courier_id == courier.id


def one_order_two_couriers():
    db_session = get_session()

    # создает один заказ и 2 курьеров - один далеко от ресторана, другой близко
    order = ...
    far_courier = ...
    close_courier = ...

    # пытается назначить
    assign_orders(db_session, order, [far_courier, close_courier])

    # проверяет что был назначен именно близкий курьер
    assignment = ... # Select order_assignment where order_id == order.id
    assert assignment.courier_id == close_courier.id

# и.т.д.