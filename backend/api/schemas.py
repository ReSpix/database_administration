from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class OrderItemCreate(BaseModel):
    menu_item_id: int = Field(gt=0)
    quantity: int = Field(gt=0)


class OrderCreate(BaseModel):
    client_id: int = Field(gt=0)
    restaurant_id: int = Field(gt=0)
    address: str = Field(min_length=1)
    longitude: Decimal = Field(ge=-180, le=180)
    latitude: Decimal = Field(ge=-90, le=90)
    items: list[OrderItemCreate] = Field(min_length=1)


class MenuItemCreate(BaseModel):
    dish_id: int = Field(gt=0)
    price: Decimal = Field(gt=0, decimal_places=2)


class MenuItemUpdate(BaseModel):
    price: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    available: bool | None = None


class OrderReadyUpdate(BaseModel):
    estimated_ready_at: datetime | None = None


class CourierLocationUpdate(BaseModel):
    longitude: Decimal = Field(ge=-180, le=180)
    latitude: Decimal = Field(ge=-90, le=90)
