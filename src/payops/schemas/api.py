"""Request and response models for the HTTP API."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from payops.db.models.enums import (
    ActionStatus,
    ActionType,
    ApprovalStatus,
    InvoiceStatus,
    RunStatus,
    Tone,
)

Approver = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=1000)]

MAX_RUN_INVOICES = 500

_EXAMPLE_UUID = "3f2b8c1e-9a4d-4e7b-8f21-6c5d0a9e1b23"


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------- errors


class ErrorDetail(BaseModel):
    code: str = Field(examples=["validation_error"])
    message: str = Field(examples=["Invalid request"])
    details: list[dict[str, Any]] = Field(
        default_factory=list,
        examples=[[{"loc": ["body", "approver"], "msg": "Field required", "type": "missing"}]],
    )


class ErrorResponse(BaseModel):
    error: ErrorDetail


# ---------------------------------------------------------------------------- runs


class RunCreate(_Request):
    """Start an agent run over specific invoices, or over every overdue invoice."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"scope": "all_overdue", "max_invoices": 20},
                {"invoice_ids": [_EXAMPLE_UUID]},
            ]
        },
    )

    invoice_ids: list[uuid.UUID] | None = Field(
        default=None, min_length=1, max_length=MAX_RUN_INVOICES
    )
    scope: Literal["all_overdue"] | None = None
    max_invoices: int | None = Field(default=None, ge=1, le=MAX_RUN_INVOICES)

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Self:
        if (self.invoice_ids is None) == (self.scope is None):
            raise ValueError("Provide exactly one of 'invoice_ids' or 'scope'")
        if self.invoice_ids is not None and len(set(self.invoice_ids)) != len(self.invoice_ids):
            raise ValueError("'invoice_ids' contains duplicates")
        return self


class RunCreated(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"run_id": _EXAMPLE_UUID, "status": "queued"}]}
    )

    run_id: uuid.UUID
    status: RunStatus


class TraceEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    name: str
    actor: str | None
    inputs: dict[str, Any]
    outputs: Any | None
    error: str | None
    latency_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    created_at: datetime


class RunTotals(BaseModel):
    steps: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int = Field(description="Sum of LLM and tool call latencies")


class RunDetail(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": _EXAMPLE_UUID,
                    "status": "queued",
                    "scope": "all_overdue",
                    "requested_invoice_ids": None,
                    "max_invoices": 20,
                    "llm_provider": None,
                    "llm_model": None,
                    "prompt_version": None,
                    "error": None,
                    "created_at": "2026-11-15T09:00:00Z",
                    "started_at": None,
                    "finished_at": None,
                    "totals": {
                        "steps": 1,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "latency_ms": 0,
                    },
                    "trace": [
                        {
                            "id": 1,
                            "event_type": "run_created",
                            "name": "POST /runs",
                            "actor": "api",
                            "inputs": {"scope": "all_overdue", "max_invoices": 20},
                            "outputs": {"status": "queued"},
                            "error": None,
                            "latency_ms": None,
                            "prompt_tokens": None,
                            "completion_tokens": None,
                            "total_tokens": None,
                            "created_at": "2026-11-15T09:00:00Z",
                        }
                    ],
                }
            ]
        },
    )

    id: uuid.UUID
    status: RunStatus
    scope: str | None
    requested_invoice_ids: list[str] | None
    max_invoices: int | None
    llm_provider: str | None
    llm_model: str | None
    prompt_version: str | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    totals: RunTotals
    trace: list[TraceEntry]


# ---------------------------------------------------------------------------- approvals


class ApproveRequest(_Request):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"approver": "dana"}]}
    )

    approver: Approver


class RejectRequest(_Request):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"approver": "dana", "reason": "Customer already called to arrange payment"}
            ]
        },
    )

    approver: Approver
    reason: Reason


class CustomerSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    company: str
    email: str


class InvoiceSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    invoice_number: str
    amount: Decimal
    amount_paid: Decimal
    balance: Decimal
    currency: str
    status: InvoiceStatus
    due_date: date


class ActionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    action_type: ActionType
    status: ActionStatus
    tone: Tone | None
    subject: str | None
    body: str | None
    payload: dict[str, Any]


class ApprovalOut(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": _EXAMPLE_UUID,
                    "status": "pending",
                    "reasons": ["APPROVAL_OVER_THRESHOLD"],
                    "decided_by": None,
                    "decided_at": None,
                    "decision_reason": None,
                    "created_at": "2026-11-15T09:00:05Z",
                    "action": {
                        "id": _EXAMPLE_UUID,
                        "run_id": _EXAMPLE_UUID,
                        "action_type": "reminder",
                        "status": "proposed",
                        "tone": "firm",
                        "subject": "Invoice INV-00042 is 21 days overdue",
                        "body": "Hi Jordan, our records show invoice INV-00042 ...",
                        "payload": {},
                    },
                    "invoice": {
                        "id": _EXAMPLE_UUID,
                        "invoice_number": "INV-00042",
                        "amount": "24500.00",
                        "amount_paid": "0.00",
                        "balance": "24500.00",
                        "currency": "USD",
                        "status": "overdue",
                        "due_date": "2026-10-25",
                    },
                    "customer": {
                        "id": _EXAMPLE_UUID,
                        "name": "Jordan Lee",
                        "company": "Acme Logistics",
                        "email": "jordan.lee.12@example.com",
                    },
                }
            ]
        },
    )

    id: uuid.UUID
    status: ApprovalStatus
    reasons: list[str]
    decided_by: str | None
    decided_at: datetime | None
    decision_reason: str | None
    created_at: datetime
    action: ActionSummary
    invoice: InvoiceSummary
    customer: CustomerSummary


class ApprovalPage(BaseModel):
    items: list[ApprovalOut]
    next_cursor: str | None = Field(description="Pass as `cursor` to get the next page.")


# ---------------------------------------------------------------------------- audit


class AuditEntry(TraceEntry):
    run_id: uuid.UUID | None


class AuditPage(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "items": [
                        {
                            "id": 42,
                            "run_id": _EXAMPLE_UUID,
                            "event_type": "tool_call",
                            "name": "get_invoices",
                            "actor": "agent",
                            "inputs": {"customer_id": _EXAMPLE_UUID, "status": "overdue"},
                            "outputs": {"invoices": [], "total_balance": "0.00"},
                            "error": None,
                            "latency_ms": 4,
                            "prompt_tokens": None,
                            "completion_tokens": None,
                            "total_tokens": None,
                            "created_at": "2026-11-15T09:00:01Z",
                        }
                    ],
                    "next_cursor": "eyJpZCI6NDJ9",
                }
            ]
        }
    )

    items: list[AuditEntry]
    next_cursor: str | None = Field(description="Pass as `cursor` to get the next page.")
