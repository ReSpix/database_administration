"""Общие фикстуры и хелперы."""
import uuid
import os
from datetime import datetime, timezone
from decimal import Decimal

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/database_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from backend.database.session import engine as _app_engine, get_db_session
from backend.main import app


def pytest_configure(config):
    config.addinivalue_line("markers", "unit: pure functions, no I/O")
    config.addinivalue_line("markers", "contract: HTTP layer, mocked internals")
    config.addinivalue_line("markers", "integration: HTTP with real DB + fakeredis")
    config.addinivalue_line("markers", "concurrency: racing operations on real DB")


# ---------------------------------------------------------------------------
# Отдельный тестовый движок с NullPool — изолирован от app-движка
# и не держит соединений между event-loop'ами.
# ---------------------------------------------------------------------------
_test_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)


@pytest.fixture(scope="session", autouse=True)
async def _reset_test_db():
    """TRUNCATE всех таблиц + RESTART IDENTITY.

    Выполняется один раз за сессию, до всех тестов.
    Гарантирует, что тесты не унаследуют данные от предыдущего прогона
    и что последовательности идут с 1 (никаких duplicate key).
    """
    async with _test_engine.begin() as conn:
        # Список таблиц — из pg_tables, чтобы не хардкодить модели.
        tables = (await conn.execute(text(
            "select tablename from pg_tables where schemaname='public'"
        ))).scalars().all()

        if tables:
            quoted = ", ".join(f'"{t}"' for t in tables)
            await conn.execute(text(
                f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"
            ))

    yield

    await _test_engine.dispose()

# ---------------------------------------------------------------------------
# Интроспекция моделей: подставляем только те поля, что реально есть
# ---------------------------------------------------------------------------
def _filtered(model, **kwargs):
    cols = {c.key for c in sa_inspect(model).mapper.columns}
    return {k: v for k, v in kwargs.items() if k in cols}


def _require_valid(model, payload: dict, purpose: str):
    assert payload, (
        f"Ни одно из полей {purpose} не подошло для {model.__name__}. "
        f"Проверьте имена колонок в модели."
    )
    return payload


async def _persist(db_session, obj):
    db_session.add(obj)
    await db_session.flush()
    await db_session.refresh(obj)
    return obj


# ---------------------------------------------------------------------------
# Сессия: savepoint-откат после каждого теста
# ---------------------------------------------------------------------------
@pytest.fixture
async def db_session():
    connection = await _test_engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


@pytest.fixture
async def integration_client(db_session, monkeypatch):
    import fakeredis.aioredis as fakeaioredis
    fake_redis = fakeaioredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr("backend.courier_locations.redis_client", fake_redis)

    async def override():
        yield db_session

    app.dependency_overrides[get_db_session] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        await fake_redis.aclose()


# ---------------------------------------------------------------------------
# Доменные фикстуры
# ---------------------------------------------------------------------------
@pytest.fixture
async def brand(db_session):
    from backend.database.models import Brand
    tag = uuid.uuid4().hex[:6]
    payload = _filtered(
        Brand,
        name=f"Brand-{tag}",
        title=f"Brand-{tag}",
        description=None,
    )
    return await _persist(db_session, Brand(**_require_valid(Brand, payload, "brand")))


@pytest.fixture
async def other_brand(db_session):
    from backend.database.models import Brand
    tag = uuid.uuid4().hex[:6]
    payload = _filtered(
        Brand,
        name=f"OtherBrand-{tag}",
        title=f"OtherBrand-{tag}",
        description=None,
    )
    return await _persist(db_session, Brand(**_require_valid(Brand, payload, "other_brand")))

def _make_payload(model, **candidates):
    mapper = sa_inspect(model).mapper
    cols = {c.key: c for c in mapper.columns}

    payload = {k: v for k, v in candidates.items() if k in cols}

    unfilled = []
    for c in mapper.columns:
        if c.key in payload:
            continue
        if c.primary_key or c.nullable:
            continue
        if c.default is not None or c.server_default is not None:
            continue
        if c.autoincrement is True:
            continue

        if c.foreign_keys:
            targets = [fk.target_fullname for fk in c.foreign_keys]
            unfilled.append(f"{c.key} (FK → {', '.join(targets)})")
            continue

        py_type = c.type.python_type
        defaults = {
            str: f"test-{c.key}",
            int: 1,
            float: 0.0,
            bool: False,
            datetime: datetime.now(timezone.utc),
            Decimal: Decimal("0.00"),
        }
        if py_type not in defaults:
            unfilled.append(f"{c.key} (type {c.type!r})")
            continue
        payload[c.key] = defaults[py_type]

    if unfilled:
        all_cols = ", ".join(sorted(cols))
        raise AssertionError(
            f"Не могу собрать {model.__name__}: не заполнены required-поля:\n"
            + "\n".join(f"  - {u}" for u in unfilled)
            + f"\nВсе колонки: {all_cols}"
        )
    return payload


async def _make_restaurant(db_session, brand, lon=37.6173, lat=55.7558):
    from backend.database.models import Restaurant
    payload = _make_payload(
        Restaurant,
        brand_id=brand.id,
        address="Main street 1",
        longitude=lon,
        latitude=lat,
    )
    return await _persist(db_session, Restaurant(**payload))


@pytest.fixture
async def restaurant(db_session, brand):
    return await _make_restaurant(db_session, brand)


@pytest.fixture
async def other_restaurant(db_session, brand):
    return await _make_restaurant(db_session, brand, lon=37.62, lat=55.76)


@pytest.fixture
async def foreign_restaurant(db_session, other_brand):
    return await _make_restaurant(db_session, other_brand)


async def _make_dish(db_session, brand):
    from backend.database.models import Dish
    tag = uuid.uuid4().hex[:6]
    payload = _filtered(
        Dish,
        brand_id=brand.id,
        name=f"Dish-{tag}",
        title=f"Dish-{tag}",
        description=None,
        price=500,
    )
    return await _persist(db_session, Dish(**_require_valid(Dish, payload, "dish")))


async def _make_menu_item(db_session, restaurant, dish, price):
    from backend.database.models import MenuItem
    payload = _make_payload(
        MenuItem,
        restaurant_id=restaurant.id,
        dish_id=dish.id,
        price=price,
    )
    return await _persist(db_session, MenuItem(**payload))


@pytest.fixture
async def menu_item_1(db_session, restaurant, brand):
    dish = await _make_dish(db_session, brand)
    return await _make_menu_item(db_session, restaurant, dish, price=500)


@pytest.fixture
async def menu_item_2(db_session, restaurant, brand):
    dish = await _make_dish(db_session, brand)
    return await _make_menu_item(db_session, restaurant, dish, price=300)


@pytest.fixture
async def menu_item_other_restaurant(db_session, other_restaurant, brand):
    dish = await _make_dish(db_session, brand)
    return await _make_menu_item(db_session, other_restaurant, dish, price=400)


@pytest.fixture
async def menu_item_foreign_brand(db_session, foreign_restaurant, other_brand):
    dish = await _make_dish(db_session, other_brand)
    return await _make_menu_item(db_session, foreign_restaurant, dish, price=200)


@pytest.fixture
async def auth_provider(db_session):
    from backend.database.models import AuthProvider
    tag = uuid.uuid4().hex[:6]
    return await _persist(
        db_session,
        AuthProvider(**_make_payload(AuthProvider, name=f"Provider-{tag}")),
    )


@pytest.fixture
async def client_user(db_session, auth_provider):
    from backend.database.models import Client
    tag = uuid.uuid4().hex[:6]
    return await _persist(db_session, Client(**_make_payload(
        Client,
        auth_provider_id=auth_provider.id,
        external_id=f"external-{tag}",
    )))


async def _make_courier(db_session, suffix):
    from backend.database.models import Courier
    tag = uuid.uuid4().hex[:6]
    payload = _make_payload(
        Courier,
        name=f"Courier{suffix}-{tag}",
    )
    return await _persist(db_session, Courier(**payload))


@pytest.fixture
async def courier(db_session):
    return await _make_courier(db_session, "A")


@pytest.fixture
async def courier_2(db_session):
    return await _make_courier(db_session, "B")


@pytest.fixture
async def courier_3(db_session):
    return await _make_courier(db_session, "C")


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------
async def create_order(client, *, client_id, restaurant_id, items,
                       lon=37.6173, lat=55.7558):
    return await client.post("/orders", json={
        "client_id": client_id,
        "restaurant_id": restaurant_id,
        "address": "Main street 1",
        "longitude": lon, "latitude": lat,
        "items": [{"menu_item_id": m, "quantity": q} for m, q in items],
    })


async def make_ready_order(client, client_id, restaurant, menu_item):
    r = await create_order(
        client, client_id=client_id, restaurant_id=restaurant.id,
        items=[(menu_item.id, 1)],
    )
    assert r.status_code == 201, r.text
    order_id = r.json()["id"]
    await client.post(f"/restaurants/{restaurant.id}/orders/{order_id}/accept")
    await client.post(f"/restaurants/{restaurant.id}/orders/{order_id}/ready",
                      json={})
    return order_id