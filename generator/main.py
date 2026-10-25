from .database import get_session
from .init_database import fill_database
from .test_cases import (
    one_order_one_courier,
    one_order_two_couriers,
)  # итд, либо можно как-то обернуть


def main():
    session = get_session()
    fill_database(session)

    one_order_one_courier()
    one_order_two_couriers()
    # итд


if __name__ == "__main__":
    main()
