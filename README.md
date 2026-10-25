# Food Delivery API

Минимальная основа API сервиса доставки еды на FastAPI с асинхронными
сессиями SQLAlchemy и PostgreSQL.

## Запуск

Установить зависимости и запустить PostgreSQL и Redis:

```powershell
uv sync
docker compose up -d postgres redis
```

Запустить API из корня проекта:

```powershell
uv run python -m backend.init_db
uv run uvicorn backend.main:app --reload
```

Или запустить API вместе с PostgreSQL и Redis в Docker:

```powershell
docker compose up --build backend
```

При запуске через Docker Compose сервис `db-init` автоматически создаёт таблицы
и тестовые данные после готовности PostgreSQL и до запуска API. При локальном
запуске сначала выполните `backend.init_db`, как показано выше.

После запуска API будет доступен по адресу `http://localhost:8000`; интерактивная
документация — `http://localhost:8000/docs`. Сборки сервисов описаны в
`backend/Dockerfile` и `generator/Dockerfile`.

По умолчанию используется `postgresql+asyncpg://postgres:postgres@localhost:5432/database`.
Для другой базы задайте переменную окружения `DATABASE_URL` в формате SQLAlchemy
для асинхронного драйвера.

Redis доступен по умолчанию по адресу `redis://localhost:6379/0`. Для изменения
адреса задайте `REDIS_URL`. Координаты курьеров записываются в Redis GEO с помощью
`save_courier_location`, а `find_nearby_couriers` возвращает ID ближайших курьеров
в заданном радиусе в километрах (по умолчанию не более 10).

## API

Первая версия использует ID клиентов, ресторанов и курьеров в URL или теле
запроса; авторизации пока нет.

- `GET /brands`, `GET /restaurants`, `GET /dishes` — каталог; рестораны можно
  отфильтровать по `brand_id`, блюда — по `brand_id` и/или `restaurant_id`.
- `POST /orders` — создать заказ для существующего клиента. Тело содержит
  `client_id`, `restaurant_id`, адрес доставки, координаты и список
  `menu_item_id` с количеством.
- `GET /clients/{client_id}/orders/{order_id}/status` — статус заказа клиента.
- `GET /restaurants/{restaurant_id}/menu-items` — меню, включая недоступные
  позиции; `POST /restaurants/{restaurant_id}/menu-items` добавляет блюдо бренда
  в меню, `PATCH /restaurants/{restaurant_id}/menu-items/{menu_item_id}` меняет
  цену и/или доступность.
- `POST /restaurants/{restaurant_id}/orders/{order_id}/accept` и
  `POST /restaurants/{restaurant_id}/orders/{order_id}/ready` — принять заказ
  и отметить его готовым. В запросе `ready` можно передать
  `estimated_ready_at`.
- `GET /restaurants/{restaurant_id}/couriers/nearby` — ближайшие курьеры из
  Redis GEO; доступны параметры `radius_km` и `limit`.
- `PUT /couriers/{courier_id}/location` — обновить координаты курьера в Redis.
- `GET /couriers/{courier_id}/orders/available` — доступные готовые заказы;
  `POST /couriers/{courier_id}/orders/{order_id}/accept`, `/pickup` и `/deliver`
  — принять доставку, забрать и подтвердить доставку.

Статусы заказа переходят по цепочке `new` → `accepted` → `ready` → `assigned` →
`picked_up` → `delivered`. Первая операция создаёт используемые записи статусов,
если их ещё нет в таблице.

## Тесты

Установить тестовые зависимости и запустить тесты:

```powershell
uv sync --group dev
uv run pytest
```

Проверка доступности API: `GET http://127.0.0.1:8000/health`.
Документация OpenAPI доступна по адресу `http://127.0.0.1:8000/docs`.
