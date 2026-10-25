# Food Delivery Simulation

Симуляция сервиса доставки еды с тремя типами акторов:
- **Клиенты** — создают заказы, отслеживают статус, могут отменить
- **Рестораны** — принимают/отклоняют заказы, сообщают о готовности
- **Курьеры** — получают офферы (по одному заказу), принимают/отклоняют, завершают доставку

## Архитектура

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│   Client    │────▶│                  │◀───▶│ Restaurant  │
│  Emulator   │     │  Backend API     │     │  Emulator   │
└─────────────┘     │  (dispatch-service)       └─────────────┘
                    │                  │
                    │                  │     ┌─────────────┐
                    └──────────────────┴────▶│  Courier    │
                          ▲                  │  Emulator   │
                          │                  └─────────────┘
                          │
                    ┌─────┴─────┐
                    │  Metrics  │
                    └───────────┘
```

### API Endpoints (expected from backend)

**Client:**
- `POST /v1/orders` — создать заказ
- `GET /v1/orders/{orderId}` — получить статус
- `POST /v1/orders/{orderId}/cancel` — отменить заказ

**Restaurant:**
- `GET /v1/restaurant/orders` — получить новые заказы
- `POST /v1/restaurant/orders/{orderId}/accept` — принять
- `POST /v1/restaurant/orders/{orderId}/reject` — отклонить
- `POST /v1/restaurant/orders/{orderId}/ready` — заказ готов (критично для диспетчеризации)

**Courier:**
- `PATCH /v1/couriers/{courierId}/status` — heartbeat (статус + геолокация)
- `GET /v1/couriers/{courierId}/offers` — опрос офферов (polling)
- `GET /v1/couriers/{courierId}/offers/stream` — SSE поток офферов
- `POST /v1/couriers/{courierId}/offers/{offerId}/accept` — принять оффер
- `POST /v1/couriers/{courierId}/offers/{offerId}/decline` — отклонить оффер
- `POST /v1/couriers/{courierId}/tasks/{orderId}/complete-pickup` — забрал заказ
- `POST /v1/couriers/{courierId}/tasks/{orderId}/complete-delivery` — доставил
- `POST /v1/couriers/{courierId}/tasks/{orderId}/fail` — не смог доставить

### Важно: Один заказ на курьера

Курьеры **не батчат** заказы. Каждый оффер содержит ровно один заказ:
```
route = [
  {"type": "pickup", "orderId": "...", "location": {...}},
  {"type": "dropoff", "orderId": "...", "location": {...}}
]
```

## Быстрый старт

### 1. Запуск с Docker Compose (требуется запущенный backend)

```bash
# Отредактируйте .env - укажите правильный API_HOST
# Например: API_HOST=dispatch-service (если backend в той же сети)
# Или: API_HOST=host.docker.internal (если backend на хост-машине)

docker compose up --build sim
```

### 2. Локальный запуск (для разработки)

```bash
# Установите зависимости
pip install -r requirements.txt

# Настройте .env для локального backend
# API_HOST=localhost

# Запустите симуляцию
python -m generator.main
```

### 3. Переменные окружения (.env)

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| `API_SCHEME` | `http` или `https` | `http` |
| `API_HOST` | Хост backend API | `dispatch-service` |
| `API_PORT` | Порт backend API | `8080` |
| `API_TIMEOUT` | Таймаут запросов (сек) | `5.0` |
| `API_HEALTH_PATH` | Health-check эндпоинт | `/health` |
| `API_MAX_RETRIES` | Кол-во ретраев | `3` |
| `API_RETRY_BACKOFF` | Бэкофф ретраев (сек) | `0.5` |
| `SIM_SEED` | Seed для воспроизводимости | `42` |
| `SIM_DURATION` | Длительность симуляции (сек) | `3600` |
| `SIM_ORDER_RATE` | Заказов в секунду (Poisson) | `0.2` |
| `SIM_COURIER_TICK` | Тик курьера (сек) | `2.0` |
| `SIM_PREFLIGHT` | Проверять health перед стартом | `true` |

## Структура проекта

```
generator/
├── main.py          # Точка входа
├── config.py        # Настройки через pydantic-settings
├── api.py           # HTTP клиент + MockApiClient для тестов
├── core.py          # Point, EventQueue, SeededRNG
├── entities.py      # Client, Restaurant, Courier, Order, OrderStatus
├── emulators.py     # ClientEmulator, RestaurantEmulator, CourierEmulator
├── engine.py        # SimulationEngine (event-driven)
├── metrics.py       # Сбор метрик
└── Dockerfile
```

## Метрики

В конце симуляции выводится JSON с метриками:

```json
{
  "orders_created": 720,
  "orders_accepted": 685,
  "orders_rejected": 35,
  "orders_delivered": 650,
  "offer_accepts": 650,
  "offer_declines": 42,
  "accept_rate": 0.951,
  "delivery_time_sec": {"count": 650, "mean": 1245.3, "p50": 1180.0, "p95": 1890.0, "p99": 2340.0},
  "prep_time_sec": {"count": 685, "mean": 342.1, "p50": 320.0, "p95": 480.0, "p99": 590.0},
  "ready_to_pickup_sec": {"count": 650, "mean": 45.2, "p50": 38.0, "p95": 85.0, "p99": 120.0},
  "batch_size": {"count": 650, "mean": 1.0, "p50": 1.0, "p95": 1.0, "p99": 1.0}
}
```

## Тестирование без backend (MockApiClient)

Для unit-тестов движка есть `MockApiClient` в `api.py` — он эмулирует backend в памяти:

```python
from generator.api import MockApiClient
from generator.main import _build_world
from generator.core import SeededRNG
from generator.engine import SimulationEngine

api = MockApiClient()
# ... регистрируйте рестораны в api.restaurant_positions ...
engine = SimulationEngine(api=api, clients, restaurants, couriers, rng)
metrics = await engine.run()
```

## Особенности реализации

1. **Event-driven симуляция** — все события (приход заказа, решение ресторана, готовность, тик курьера) идут через приоритетную очередь по времени.

2. **Детерминированность** — `SeededRNG` гарантирует воспроизводимые прогоны при одном `SIM_SEED`.

3. **Курьеры не батчат** — `batch_size` всегда равен 1. Оффер выдается только IDLE курьеру, после принятия курьер становится BUSY до завершения доставки.

4. **Health-check** — перед стартом симуляция проверяет `GET {API_HEALTH_PATH}` (настройка `SIM_PREFLIGHT`).

5. **Ретраи** — HTTP клиент делает ретраи на транспортные ошибки и 5xx с экспоненциальным бэкоффом.