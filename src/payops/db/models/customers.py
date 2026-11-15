import uuid

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from payops.db.base import Base, TimestampMixin, pg_enum
from payops.db.models.enums import ScenarioType


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    company: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    phone: Mapped[str | None] = mapped_column(String(50))
    scenario_type: Mapped[ScenarioType] = mapped_column(
        pg_enum(ScenarioType, "scenario_type"), index=True
    )

    invoices: Mapped[list["Invoice"]] = relationship(back_populates="customer")  # noqa: F821
