import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from payops import clock
from payops.db.models import Customer, Invoice
from payops.db.models.enums import InvoiceStatus
from payops.errors import NotFoundError
from payops.services.audit import audited


class GetInvoicesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: uuid.UUID
    status: InvoiceStatus | None = None


class InvoiceOut(BaseModel):
    id: uuid.UUID
    invoice_number: str
    amount: Decimal
    amount_paid: Decimal
    balance: Decimal
    currency: str
    status: InvoiceStatus
    issue_date: date
    due_date: date
    days_overdue: int


class GetInvoicesOutput(BaseModel):
    customer_id: uuid.UUID
    invoices: list[InvoiceOut]
    total_balance: Decimal


def days_overdue(invoice: Invoice, today: date) -> int:
    if invoice.status is InvoiceStatus.PAID:
        return 0
    return max(0, (today - invoice.due_date).days)


@audited(name="get_invoices")
def get_invoices(session: Session, inp: GetInvoicesInput) -> GetInvoicesOutput:
    """List a customer's invoices (optionally filtered by status), oldest due date first."""
    if session.get(Customer, inp.customer_id) is None:
        raise NotFoundError(f"Customer {inp.customer_id} not found")
    stmt = select(Invoice).where(Invoice.customer_id == inp.customer_id)
    if inp.status is not None:
        stmt = stmt.where(Invoice.status == inp.status)
    today = clock.today()
    invoices = [
        InvoiceOut(
            id=inv.id,
            invoice_number=inv.invoice_number,
            amount=inv.amount,
            amount_paid=inv.amount_paid,
            balance=inv.balance,
            currency=inv.currency,
            status=inv.status,
            issue_date=inv.issue_date,
            due_date=inv.due_date,
            days_overdue=days_overdue(inv, today),
        )
        for inv in session.scalars(stmt.order_by(Invoice.due_date, Invoice.invoice_number))
    ]
    return GetInvoicesOutput(
        customer_id=inp.customer_id,
        invoices=invoices,
        total_balance=sum((i.balance for i in invoices), Decimal("0.00")),
    )
