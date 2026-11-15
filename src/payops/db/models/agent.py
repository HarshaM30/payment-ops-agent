import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from payops.db.base import Base, TimestampMixin, pg_enum
from payops.db.models.billing import Invoice
from payops.db.models.enums import ActionStatus, ActionType, ApprovalStatus, RunStatus, Tone


class AgentRun(TimestampMixin, Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    status: Mapped[RunStatus] = mapped_column(
        pg_enum(RunStatus, "run_status"), default=RunStatus.QUEUED, index=True
    )
    scope: Mapped[str | None] = mapped_column(String(50))
    requested_invoice_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    max_invoices: Mapped[int | None] = mapped_column(Integer)
    llm_provider: Mapped[str | None] = mapped_column(String(50))
    llm_model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    actions: Mapped[list["Action"]] = relationship(back_populates="run")


class Action(TimestampMixin, Base):
    __tablename__ = "actions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    action_type: Mapped[ActionType] = mapped_column(pg_enum(ActionType, "action_type"))
    status: Mapped[ActionStatus] = mapped_column(
        pg_enum(ActionStatus, "action_status"), default=ActionStatus.PROPOSED
    )
    tone: Mapped[Tone | None] = mapped_column(pg_enum(Tone, "message_tone"))
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    executed_at: Mapped[datetime | None]

    run: Mapped[AgentRun] = relationship(back_populates="actions")
    invoice: Mapped[Invoice] = relationship()
    approval: Mapped["Approval | None"] = relationship(back_populates="action")


class Approval(TimestampMixin, Base):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    action_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("actions.id"), unique=True)
    status: Mapped[ApprovalStatus] = mapped_column(
        pg_enum(ApprovalStatus, "approval_status"), default=ApprovalStatus.PENDING, index=True
    )
    # Policy rule ids that required this approval, e.g. ["APPROVAL_OVER_THRESHOLD"].
    reasons: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), default=list
    )
    decided_by: Mapped[str | None] = mapped_column(String(100))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None]

    action: Mapped[Action] = relationship(back_populates="approval")
