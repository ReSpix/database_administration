from sqlalchemy import ForeignKey, Text, Integer, UniqueConstraint, String, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# Тут будут все ORM модели
