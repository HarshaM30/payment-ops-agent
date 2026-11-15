"""Add an idempotency key (and request hash) to agent_runs for POST /runs.

Revision ID: 044b4e133a69
Revises: dad6f9e2ee12
Create Date: 2026-11-15 04:34:25.220224
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "044b4e133a69"
down_revision: str | None = "dad6f9e2ee12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("idempotency_key", sa.String(length=200), nullable=True))
    op.add_column("agent_runs", sa.Column("request_hash", sa.String(length=64), nullable=True))
    op.create_unique_constraint(
        op.f("uq_agent_runs_idempotency_key"), "agent_runs", ["idempotency_key"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_agent_runs_idempotency_key"), "agent_runs", type_="unique")
    op.drop_column("agent_runs", "request_hash")
    op.drop_column("agent_runs", "idempotency_key")
