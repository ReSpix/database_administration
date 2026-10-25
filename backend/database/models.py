from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Базовый декларативный класс."""


# ─────────────────────────── Brand / Dishes ───────────────────────────


class Brand(Base):
    __tablename__ = "brands"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String, nullable=True)

    restaurants: Mapped[list[Restaurant]] = relationship(back_populates="brand")
    dishes: Mapped[list[Dish]] = relationship(back_populates="brand")


class Dish(Base):
    __tablename__ = "dishes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"))
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String, nullable=True)

    brand: Mapped[Brand] = relationship(back_populates="dishes")
    menu_items: Mapped[list[MenuItem]] = relationship(back_populates="dish")
    tag_assignments: Mapped[list[TagAssignment]] = relationship(back_populates="dish")


# ─────────────────────────── Tag / Tag assignment ───────────────────────────


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String)

    assignments: Mapped[list[TagAssignment]] = relationship(back_populates="tag")


class TagAssignment(Base):
    __tablename__ = "tag_assignments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dish_id: Mapped[int] = mapped_column(ForeignKey("dishes.id"))
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id"))

    dish: Mapped[Dish] = relationship(back_populates="tag_assignments")
    tag: Mapped[Tag] = relationship(back_populates="assignments")


# ─────────────────────────── Restaurant / Menu item / Stop list ───────────────────────────


class Restaurant(Base):
    __tablename__ = "restaurants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"))
    address: Mapped[str] = mapped_column(String)
    longitude: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6))

    brand: Mapped[Brand] = relationship(back_populates="restaurants")
    menu_items: Mapped[list[MenuItem]] = relationship(back_populates="restaurant")
    orders: Mapped[list[Order]] = relationship(back_populates="restaurant")


class MenuItem(Base):
    __tablename__ = "menu_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurants.id"))
    dish_id: Mapped[int] = mapped_column(ForeignKey("dishes.id"))
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    restaurant: Mapped[Restaurant] = relationship(back_populates="menu_items")
    dish: Mapped[Dish] = relationship(back_populates="menu_items")
    stop_list_entries: Mapped[list[StopList]] = relationship(back_populates="menu_item")
    order_items: Mapped[list[OrderItem]] = relationship(back_populates="menu_item")


class StopList(Base):
    __tablename__ = "stop_lists"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    menu_item_id: Mapped[int] = mapped_column(ForeignKey("menu_items.id"))
    reason: Mapped[str] = mapped_column(String)
    stopped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    menu_item: Mapped[MenuItem] = relationship(back_populates="stop_list_entries")


# ─────────────────────────── Order / Order status / Order item ───────────────────────────


class OrderStatus(Base):
    __tablename__ = "order_statuses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String)

    orders: Mapped[list[Order]] = relationship(back_populates="status")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    status_id: Mapped[int] = mapped_column(ForeignKey("order_statuses.id"))
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurants.id"))
    address: Mapped[str] = mapped_column(String)
    longitude: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    estimated_ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    client: Mapped[Client] = relationship(back_populates="orders")
    status: Mapped[OrderStatus] = relationship(back_populates="orders")
    restaurant: Mapped[Restaurant] = relationship(back_populates="orders")
    items: Mapped[list[OrderItem]] = relationship(back_populates="order")
    assignments: Mapped[list[OrderAssignment]] = relationship(back_populates="order")
    ratings: Mapped[list[Rating]] = relationship(back_populates="order")
    disputes: Mapped[list[Dispute]] = relationship(back_populates="order")
    payments: Mapped[list[Payment]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    menu_item_id: Mapped[int] = mapped_column(ForeignKey("menu_items.id"))
    amount: Mapped[int] = mapped_column(Integer)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    order: Mapped[Order] = relationship(back_populates="items")
    menu_item: Mapped[MenuItem] = relationship(back_populates="order_items")


# ─────────────────────────── Courier / Order assignment ───────────────────────────


class Courier(Base):
    __tablename__ = "couriers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String)

    assignments: Mapped[list[OrderAssignment]] = relationship(back_populates="courier")


class OrderAssignment(Base):
    __tablename__ = "order_assignments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    courier_id: Mapped[int] = mapped_column(ForeignKey("couriers.id"))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    picked_up_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    order: Mapped[Order] = relationship(back_populates="assignments")
    courier: Mapped[Courier] = relationship(back_populates="assignments")


# ─────────────────────────── Rating ───────────────────────────


class Rating(Base):
    __tablename__ = "ratings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    courier_score: Mapped[int] = mapped_column(Integer)
    restaurant_score: Mapped[int] = mapped_column(Integer)

    order: Mapped[Order] = relationship(back_populates="ratings")


# ─────────────────────────── Dispute / Dispute type / Dispute result / Proof ───────────────────────────


class DisputeType(Base):
    __tablename__ = "dispute_types"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String)

    disputes: Mapped[list[Dispute]] = relationship(back_populates="dispute_type")


class DisputeResult(Base):
    __tablename__ = "dispute_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String)

    disputes: Mapped[list[Dispute]] = relationship(back_populates="result_type")


class Dispute(Base):
    __tablename__ = "disputes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    result_type_id: Mapped[int] = mapped_column(ForeignKey("dispute_results.id"))
    dispute_type_id: Mapped[int] = mapped_column(ForeignKey("dispute_types.id"))
    reason: Mapped[str] = mapped_column(String)
    open_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    order: Mapped[Order] = relationship(back_populates="disputes")
    result_type: Mapped[DisputeResult] = relationship(back_populates="disputes")
    dispute_type: Mapped[DisputeType] = relationship(back_populates="disputes")
    proofs: Mapped[list[Proof]] = relationship(back_populates="dispute")


class Proof(Base):
    __tablename__ = "proofs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dispute_id: Mapped[int] = mapped_column(ForeignKey("disputes.id"))
    # id объекта в S3, чтобы связать доказательство со спором
    external_id: Mapped[str] = mapped_column(String)

    dispute: Mapped[Dispute] = relationship(back_populates="proofs")


# ─────────────────────────── Payment / Payment status ───────────────────────────


class PaymentStatus(Base):
    __tablename__ = "payment_statuses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String)

    payments: Mapped[list[Payment]] = relationship(back_populates="status")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    # В диаграмме поле названо `status: int` с пометкой FK → payment_statuses
    status_id: Mapped[int] = mapped_column(ForeignKey("payment_statuses.id"))
    external_id: Mapped[str] = mapped_column(String)

    order: Mapped[Order] = relationship(back_populates="payments")
    status: Mapped[PaymentStatus] = relationship(back_populates="payments")


# ─────────────────────────── Client / Auth provider ───────────────────────────


class AuthProvider(Base):
    __tablename__ = "auth_providers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String)

    clients: Mapped[list[Client]] = relationship(back_populates="auth_provider")


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    auth_provider_id: Mapped[int] = mapped_column(ForeignKey("auth_providers.id"))
    external_id: Mapped[str] = mapped_column(String)

    auth_provider: Mapped[AuthProvider] = relationship(back_populates="clients")
    orders: Mapped[list[Order]] = relationship(back_populates="client")
