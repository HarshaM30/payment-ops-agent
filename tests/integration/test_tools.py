"""Every tool: happy path, not-found, invalid input, and exactly one audit row per call."""

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payops.db.models import Action, AuditLog, Outbox
from payops.db.models.enums import ActionStatus, ActionType, InvoiceStatus, PaymentEventType
from payops.errors import InvalidInputError, NotFoundError
from payops.tools.actions import draft_message, flag_for_review, schedule_followup
from payops.tools.customers import get_customer
from payops.tools.history import get_payment_history
from payops.tools.invoices import get_invoices
from payops.tools.registry import call_tool
from tests.integration import factories as f

TODAY = date.today()


def audit_count(s: Session, name: str) -> int:
    return s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.name == name)) or 0


def assert_one_audit_row(s: Session, name: str, call: Callable[[], Any]) -> Any:
    before = audit_count(s, name)
    try:
        return call()
    finally:
        assert audit_count(s, name) == before + 1, f"{name} must write exactly one audit row"


# ------------------------------------------------------------------ get_customer


def test_get_customer_happy(db_session: Session) -> None:
    c = f.customer(db_session)
    f.invoice(db_session, c, amount="1000.00", amount_paid="250.00")
    f.invoice(db_session, c, amount="500.00", status=InvoiceStatus.DISPUTED)
    f.invoice(db_session, c, amount="900.00", amount_paid="900.00", status=InvoiceStatus.PAID)
    out = assert_one_audit_row(
        db_session, "get_customer", lambda: get_customer(db_session, {"customer_id": str(c.id)})
    )
    assert out.id == c.id
    assert out.unpaid_invoice_count == 2
    assert out.total_balance == Decimal("1250.00")
    assert "scenario_type" not in out.model_dump()

    row = db_session.scalars(
        select(AuditLog).where(AuditLog.name == "get_customer").order_by(AuditLog.id.desc())
    ).first()
    assert row.event_type == "tool_call"
    assert row.inputs == {"customer_id": str(c.id)}
    assert row.outputs["total_balance"] == "1250.00"
    assert row.error is None
    assert row.latency_ms is not None


def test_get_customer_not_found(db_session: Session) -> None:
    with pytest.raises(NotFoundError):
        assert_one_audit_row(
            db_session,
            "get_customer",
            lambda: get_customer(db_session, {"customer_id": str(uuid.uuid4())}),
        )
    row = db_session.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first()
    assert row.error.startswith("NotFoundError")


@pytest.mark.parametrize(
    "bad", [{}, {"customer_id": "nope"}, {"customer_id": str(uuid.uuid4()), "x": 1}]
)
def test_get_customer_invalid(db_session: Session, bad: dict) -> None:
    with pytest.raises(InvalidInputError):
        assert_one_audit_row(db_session, "get_customer", lambda: get_customer(db_session, bad))


# ------------------------------------------------------------------ get_invoices


def test_get_invoices_happy_and_filter(db_session: Session) -> None:
    c = f.customer(db_session)
    late = f.invoice(db_session, c, amount="300.00", due_date=TODAY - timedelta(days=12))
    f.invoice(
        db_session,
        c,
        amount="200.00",
        status=InvoiceStatus.OPEN,
        due_date=TODAY + timedelta(days=5),
    )
    f.invoice(db_session, c, amount="100.00", amount_paid="100.00", status=InvoiceStatus.PAID)

    out = assert_one_audit_row(
        db_session, "get_invoices", lambda: get_invoices(db_session, {"customer_id": str(c.id)})
    )
    assert len(out.invoices) == 3
    assert out.total_balance == Decimal("500.00")
    assert [i.due_date for i in out.invoices] == sorted(i.due_date for i in out.invoices)

    overdue = get_invoices(db_session, {"customer_id": str(c.id), "status": "overdue"})
    assert [i.id for i in overdue.invoices] == [late.id]
    assert overdue.invoices[0].days_overdue == 12
    paid = get_invoices(db_session, {"customer_id": str(c.id), "status": "paid"})
    assert paid.invoices[0].days_overdue == 0


def test_get_invoices_not_found(db_session: Session) -> None:
    with pytest.raises(NotFoundError):
        assert_one_audit_row(
            db_session,
            "get_invoices",
            lambda: get_invoices(db_session, {"customer_id": str(uuid.uuid4())}),
        )


@pytest.mark.parametrize(
    "bad", [{"customer_id": "x"}, {"customer_id": str(uuid.uuid4()), "status": "late"}]
)
def test_get_invoices_invalid(db_session: Session, bad: dict) -> None:
    with pytest.raises(InvalidInputError):
        assert_one_audit_row(db_session, "get_invoices", lambda: get_invoices(db_session, bad))


# ------------------------------------------------------------------ get_payment_history


def test_get_payment_history_happy(db_session: Session) -> None:
    c = f.customer(db_session)
    on_time = f.invoice(
        db_session,
        c,
        amount="100.00",
        amount_paid="100.00",
        status=InvoiceStatus.PAID,
        due_date=TODAY - timedelta(days=60),
    )
    f.payment(db_session, on_time, "100.00", TODAY - timedelta(days=61))
    late = f.invoice(
        db_session,
        c,
        amount="200.00",
        amount_paid="200.00",
        status=InvoiceStatus.PAID,
        due_date=TODAY - timedelta(days=40),
    )
    f.payment(db_session, late, "200.00", TODAY - timedelta(days=30))
    open_inv = f.invoice(db_session, c, amount="400.00", amount_paid="100.00")
    f.payment(db_session, open_inv, "100.00", TODAY - timedelta(days=2))
    f.plan(db_session, c, open_inv)
    f.event(db_session, c, PaymentEventType.PROMISE_TO_PAY, datetime.now(UTC) - timedelta(days=1))

    out = assert_one_audit_row(
        db_session,
        "get_payment_history",
        lambda: get_payment_history(db_session, {"customer_id": str(c.id), "months": 6}),
    )
    assert out.invoices_paid == 2
    assert out.on_time_rate == 0.5
    assert out.avg_days_late == 5.0
    assert out.partial_payment_count == 1
    assert out.total_paid == Decimal("400.00")
    assert out.active_plan is not None
    assert out.active_plan.invoice_id == open_inv.id
    assert out.recent_events[0].event_type is PaymentEventType.PROMISE_TO_PAY


def test_get_payment_history_not_found(db_session: Session) -> None:
    with pytest.raises(NotFoundError):
        assert_one_audit_row(
            db_session,
            "get_payment_history",
            lambda: get_payment_history(db_session, {"customer_id": str(uuid.uuid4())}),
        )


@pytest.mark.parametrize("months", [0, 37, "many"])
def test_get_payment_history_invalid(db_session: Session, months: object) -> None:
    bad = {"customer_id": str(uuid.uuid4()), "months": months}
    with pytest.raises(InvalidInputError):
        assert_one_audit_row(
            db_session, "get_payment_history", lambda: get_payment_history(db_session, bad)
        )


# ------------------------------------------------------------------ draft_message


def _draft_args(run_id: uuid.UUID, invoice_id: uuid.UUID, **kw: object) -> dict:
    return {
        "run_id": str(run_id),
        "invoice_id": str(invoice_id),
        "action_type": "reminder",
        "tone": "friendly",
        "subject": "Reminder: invoice overdue",
        "body": "Hi, a quick reminder that your invoice is overdue.",
        **kw,
    }


def test_draft_message_stores_proposed_action_and_never_sends(db_session: Session) -> None:
    c = f.customer(db_session)
    inv = f.invoice(db_session, c)
    r = f.run(db_session)
    out = assert_one_audit_row(
        db_session,
        "draft_message",
        lambda: draft_message(db_session, _draft_args(r.id, inv.id, context="slightly late")),
    )
    assert out.status is ActionStatus.PROPOSED
    action = db_session.get(Action, out.action_id)
    assert action.body.startswith("Hi, a quick reminder")
    assert action.payload == {"context": "slightly late"}
    assert db_session.scalar(select(func.count()).select_from(Outbox)) == 0

    row = db_session.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first()
    assert row.run_id == r.id

    # Redraft replaces the proposed draft instead of adding a second one.
    again = draft_message(db_session, _draft_args(r.id, inv.id, tone="firm", body="Second draft"))
    assert again.action_id == out.action_id
    assert db_session.get(Action, out.action_id).body == "Second draft"


@pytest.mark.parametrize("missing", ["run", "invoice"])
def test_draft_message_not_found(db_session: Session, missing: str) -> None:
    inv = f.invoice(db_session, f.customer(db_session))
    r = f.run(db_session)
    args = _draft_args(
        uuid.uuid4() if missing == "run" else r.id,
        uuid.uuid4() if missing == "invoice" else inv.id,
    )
    with pytest.raises(NotFoundError):
        assert_one_audit_row(db_session, "draft_message", lambda: draft_message(db_session, args))
    assert db_session.scalar(select(func.count()).select_from(Action)) == 0


@pytest.mark.parametrize(
    "override",
    [{"action_type": "send_money"}, {"tone": "angry"}, {"body": ""}, {"subject": "x" * 201}],
)
def test_draft_message_invalid(db_session: Session, override: dict) -> None:
    args = _draft_args(uuid.uuid4(), uuid.uuid4(), **override)
    with pytest.raises(InvalidInputError):
        assert_one_audit_row(db_session, "draft_message", lambda: draft_message(db_session, args))


# ------------------------------------------------------------------ schedule_followup


def test_schedule_followup_happy(db_session: Session) -> None:
    inv = f.invoice(db_session, f.customer(db_session))
    r = f.run(db_session)
    when = TODAY + timedelta(days=7)
    out = assert_one_audit_row(
        db_session,
        "schedule_followup",
        lambda: schedule_followup(
            db_session,
            {"run_id": str(r.id), "invoice_id": str(inv.id), "followup_date": when.isoformat()},
        ),
    )
    assert out.action_type is ActionType.SCHEDULE_FOLLOWUP
    assert out.payload == {"followup_date": when.isoformat()}


def test_schedule_followup_not_found(db_session: Session) -> None:
    r = f.run(db_session)
    args = {
        "run_id": str(r.id),
        "invoice_id": str(uuid.uuid4()),
        "followup_date": str(TODAY + timedelta(days=3)),
    }
    with pytest.raises(NotFoundError):
        assert_one_audit_row(
            db_session, "schedule_followup", lambda: schedule_followup(db_session, args)
        )


@pytest.mark.parametrize("days", [0, -5, 61])
def test_schedule_followup_invalid_date(db_session: Session, days: int) -> None:
    args = {
        "run_id": str(uuid.uuid4()),
        "invoice_id": str(uuid.uuid4()),
        "followup_date": str(TODAY + timedelta(days=days)),
    }
    with pytest.raises(InvalidInputError):
        assert_one_audit_row(
            db_session, "schedule_followup", lambda: schedule_followup(db_session, args)
        )


def test_schedule_followup_rejects_paid_invoice(db_session: Session) -> None:
    inv = f.invoice(
        db_session, f.customer(db_session), amount_paid="1000.00", status=InvoiceStatus.PAID
    )
    r = f.run(db_session)
    args = {
        "run_id": str(r.id),
        "invoice_id": str(inv.id),
        "followup_date": str(TODAY + timedelta(days=3)),
    }
    with pytest.raises(InvalidInputError):
        assert_one_audit_row(
            db_session, "schedule_followup", lambda: schedule_followup(db_session, args)
        )
    assert db_session.scalar(select(func.count()).select_from(Action)) == 0


# ------------------------------------------------------------------ flag_for_review


def test_flag_for_review_happy(db_session: Session) -> None:
    inv = f.invoice(db_session, f.customer(db_session))
    r = f.run(db_session)
    out = assert_one_audit_row(
        db_session,
        "flag_for_review",
        lambda: flag_for_review(
            db_session, {"run_id": str(r.id), "invoice_id": str(inv.id), "reason": "Odd history"}
        ),
    )
    assert out.action_type is ActionType.FLAG_FOR_REVIEW
    assert out.payload == {"reason": "Odd history"}


def test_flag_for_review_not_found(db_session: Session) -> None:
    args = {"run_id": str(uuid.uuid4()), "invoice_id": str(uuid.uuid4()), "reason": "Odd history"}
    with pytest.raises(NotFoundError):
        assert_one_audit_row(
            db_session, "flag_for_review", lambda: flag_for_review(db_session, args)
        )


@pytest.mark.parametrize("reason", ["", "ab", "x" * 1001])
def test_flag_for_review_invalid(db_session: Session, reason: str) -> None:
    args = {"run_id": str(uuid.uuid4()), "invoice_id": str(uuid.uuid4()), "reason": reason}
    with pytest.raises(InvalidInputError):
        assert_one_audit_row(
            db_session, "flag_for_review", lambda: flag_for_review(db_session, args)
        )


# ------------------------------------------------------------------ registry


def test_call_tool_injects_run_id_and_ignores_llm_supplied_one(db_session: Session) -> None:
    inv = f.invoice(db_session, f.customer(db_session))
    r = f.run(db_session)
    out = call_tool(
        db_session,
        "flag_for_review",
        {"invoice_id": str(inv.id), "reason": "Check this", "run_id": str(uuid.uuid4())},
        run_id=r.id,
    )
    assert out.run_id == r.id


def test_call_tool_read_only(db_session: Session) -> None:
    c = f.customer(db_session)
    out = call_tool(
        db_session, "get_customer", {"customer_id": str(c.id)}, run_id=f.run(db_session).id
    )
    assert out.id == c.id


def test_call_unknown_tool_is_audited(db_session: Session) -> None:
    r = f.run(db_session)
    with pytest.raises(InvalidInputError):
        assert_one_audit_row(
            db_session, "send_wire", lambda: call_tool(db_session, "send_wire", {}, run_id=r.id)
        )


def test_failed_tool_rolls_back_its_writes_but_keeps_audit_row(
    db_session: Session, monkeypatch
) -> None:
    inv = f.invoice(db_session, f.customer(db_session))
    r = f.run(db_session)

    from payops.tools import actions as actions_mod

    original = actions_mod._out

    def boom(action: Action):
        raise RuntimeError("failure after insert")

    monkeypatch.setattr(actions_mod, "_out", boom)
    with pytest.raises(RuntimeError):
        assert_one_audit_row(
            db_session,
            "flag_for_review",
            lambda: flag_for_review(
                db_session, {"run_id": str(r.id), "invoice_id": str(inv.id), "reason": "abc"}
            ),
        )
    monkeypatch.setattr(actions_mod, "_out", original)
    assert db_session.scalar(select(func.count()).select_from(Action)) == 0
