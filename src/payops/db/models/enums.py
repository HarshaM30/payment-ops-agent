"""Enum types shared by models, schemas, and services."""

from enum import StrEnum


class ScenarioType(StrEnum):
    CURRENT = "current"
    RELIABLE_SLIGHTLY_LATE = "reliable_slightly_late"
    CHRONIC_LATE = "chronic_late"
    DISPUTED_INVOICE = "disputed_invoice"
    LARGE_BALANCE = "large_balance"
    PARTIAL_PAYMENTS = "partial_payments"
    RECENT_PAYMENT_PLAN = "recent_payment_plan"


class InvoiceStatus(StrEnum):
    OPEN = "open"
    PAID = "paid"
    OVERDUE = "overdue"
    DISPUTED = "disputed"


class PaymentEventType(StrEnum):
    PAYMENT_RECEIVED = "payment_received"
    PROMISE_TO_PAY = "promise_to_pay"
    PLAN_CREATED = "plan_created"
    DISPUTE_OPENED = "dispute_opened"


class PaymentPlanStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    DEFAULTED = "defaulted"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


class ActionType(StrEnum):
    REMINDER = "reminder"
    PAYMENT_PLAN_OFFER = "payment_plan_offer"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    NO_ACTION = "no_action"
    SCHEDULE_FOLLOWUP = "schedule_followup"
    FLAG_FOR_REVIEW = "flag_for_review"


MESSAGE_ACTION_TYPES = frozenset({ActionType.REMINDER, ActionType.PAYMENT_PLAN_OFFER})


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Tone(StrEnum):
    FRIENDLY = "friendly"
    NEUTRAL = "neutral"
    FIRM = "firm"
