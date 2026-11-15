import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payops.db.models import Customer, Invoice
from payops.db.models.enums import InvoiceStatus
from payops.errors import NotFoundError
from payops.services.audit import audited

UNPAID_STATUSES = (InvoiceStatus.OPEN, InvoiceStatus.OVERDUE, InvoiceStatus.DISPUTED)


class GetCustomerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: uuid.UUID


class CustomerOut(BaseModel):
    """Customer profile. The seed's `scenario_type` label is deliberately not exposed:
    it is the answer key for evals, and the agent must infer behavior from history."""

    id: uuid.UUID
    name: str
    company: str
    email: str
    unpaid_invoice_count: int
    total_balance: Decimal


@audited(name="get_customer")
def get_customer(session: Session, inp: GetCustomerInput) -> CustomerOut:
    """Look up a customer's profile and outstanding balance."""
    customer = session.get(Customer, inp.customer_id)
    if customer is None:
        raise NotFoundError(f"Customer {inp.customer_id} not found")
    count, balance = session.execute(
        select(
            func.count(), func.coalesce(func.sum(Invoice.amount - Invoice.amount_paid), 0)
        ).where(Invoice.customer_id == customer.id, Invoice.status.in_(UNPAID_STATUSES))
    ).one()
    return CustomerOut(
        id=customer.id,
        name=customer.name,
        company=customer.company,
        email=customer.email,
        unpaid_invoice_count=count,
        total_balance=Decimal(balance),
    )
