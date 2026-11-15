import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from payops.db.base import Base, TimestampMixin, pg_enum
from payops.db.models.customers import Customer
from payops.db.models.enums import InvoiceStatus, PaymentEventType, PaymentPlanStatus


class Invoice(TimestampMixin, Base):
    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("amount_paid >= 0", name="amount_paid_non_negative"),
        CheckConstraint("amount_paid <= amount", name="amount_paid_lte_amount"),
        CheckConstraint("due_date >= issue_date", name="due_after_issue"),
        Index("ix_invoices_status_due_date", "status", "due_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    invoice_number: Mapped[str] = mapped_column(String(50), unique=True)
    amount: Mapped[Decimal]
    amount_paid: Mapped[Decimal] = mapped_column(server_default=text("0"), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[InvoiceStatus] = mapped_column(pg_enum(InvoiceStatus, "invoice_status"))
    issue_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(String(500))

    customer: Mapped[Customer] = relationship(back_populates="invoices")
    payments: Mapped[list["Payment"]] = relationship(back_populates="invoice")

    @property
    def balance(self) -> Decimal:
        return self.amount - self.amount_paid


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    amount: Mapped[Decimal]
    paid_at: Mapped[datetime]
    method: Mapped[str] = mapped_column(String(30))
    reference: Mapped[str | None] = mapped_column(String(100))

    invoice: Mapped[Invoice] = relationship(back_populates="payments")


class PaymentHistory(Base):
    """Per-customer event log: payments received, promises, plans, disputes."""

    __tablename__ = "payment_history"
    __table_args__ = (Index("ix_payment_history_customer_occurred", "customer_id", "occurred_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"))
    event_type: Mapped[PaymentEventType] = mapped_column(
        pg_enum(PaymentEventType, "payment_event_type")
    )
    amount: Mapped[Decimal | None]
    occurred_at: Mapped[datetime]
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    customer: Mapped[Customer] = relationship()
    invoice: Mapped[Invoice | None] = relationship()


class PaymentPlan(TimestampMixin, Base):
    __tablename__ = "payment_plans"
    __table_args__ = (
        CheckConstraint("total_amount > 0", name="total_amount_positive"),
        CheckConstraint("installment_amount > 0", name="installment_amount_positive"),
        CheckConstraint("installment_count > 0", name="installment_count_positive"),
        CheckConstraint(
            "installments_paid >= 0 AND installments_paid <= installment_count",
            name="installments_paid_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"))
    total_amount: Mapped[Decimal]
    installment_amount: Mapped[Decimal]
    installment_count: Mapped[int] = mapped_column(Integer)
    installments_paid: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    start_date: Mapped[date] = mapped_column(Date)
    status: Mapped[PaymentPlanStatus] = mapped_column(
        pg_enum(PaymentPlanStatus, "payment_plan_status")
    )

    customer: Mapped[Customer] = relationship()
    invoice: Mapped[Invoice | None] = relationship()
