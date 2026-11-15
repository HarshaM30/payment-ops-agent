"""Tool logic that needs no database: schemas, input validation, history math."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from payops.db.models.enums import InvoiceStatus
from payops.tools import actions
from payops.tools.history import summarize
from payops.tools.registry import READ_ONLY_TOOLS, TOOLS, tool_schemas

TODAY = date(2026, 11, 15)


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("payops.clock.now", lambda: datetime(2026, 11, 15, 12, 0, tzinfo=UTC))


def test_registry_has_all_tools_with_schemas() -> None:
    assert set(TOOLS) == {
        "get_customer",
        "get_invoices",
        "get_payment_history",
        "draft_message",
        "schedule_followup",
        "flag_for_review",
    }
    schemas = {s["function"]["name"]: s["function"] for s in tool_schemas()}
    for name, fn in schemas.items():
        assert fn["description"], name
        assert fn["parameters"]["type"] == "object"
        assert "run_id" not in fn["parameters"].get("properties", {}), name
        assert "run_id" not in fn["parameters"].get("required", []), name


def test_read_only_subset() -> None:
    names = {s["function"]["name"] for s in tool_schemas(READ_ONLY_TOOLS)}
    assert names == {"get_customer", "get_invoices", "get_payment_history"}


@pytest.mark.parametrize(
    ("offset", "ok"), [(-1, False), (0, False), (1, True), (60, True), (61, False)]
)
def test_followup_date_window(offset: int, ok: bool) -> None:
    data = {
        "run_id": uuid.uuid4(),
        "invoice_id": uuid.uuid4(),
        "followup_date": TODAY + timedelta(days=offset),
    }
    if ok:
        actions.ScheduleFollowupInput.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            actions.ScheduleFollowupInput.model_validate(data)


def test_draft_rejects_non_draft_action_type_and_blank_body() -> None:
    base = {
        "run_id": uuid.uuid4(),
        "invoice_id": uuid.uuid4(),
        "tone": "friendly",
        "subject": "Hi",
        "body": "Hello",
    }
    actions.DraftMessageInput.model_validate({**base, "action_type": "reminder"})
    with pytest.raises(ValidationError):
        actions.DraftMessageInput.model_validate({**base, "action_type": "flag_for_review"})
    with pytest.raises(ValidationError):
        actions.DraftMessageInput.model_validate({**base, "action_type": "reminder", "body": "  "})


def _inv(amount: str, due: date, status: InvoiceStatus, payments: list[tuple[str, date]]):
    return SimpleNamespace(
        amount=Decimal(amount),
        due_date=due,
        status=status,
        payments=[
            SimpleNamespace(amount=Decimal(a), paid_at=datetime(d.year, d.month, d.day, tzinfo=UTC))
            for a, d in payments
        ],
    )


def test_summarize_hand_computed() -> None:
    since = datetime(2026, 1, 1, tzinfo=UTC)
    invoices = [
        # on time
        _inv("100.00", date(2026, 3, 1), InvoiceStatus.PAID, [("100.00", date(2026, 2, 28))]),
        # 10 days late, paid in two parts (both partial)
        _inv(
            "200.00",
            date(2026, 4, 1),
            InvoiceStatus.PAID,
            [("50.00", date(2026, 4, 5)), ("150.00", date(2026, 4, 11))],
        ),
        # 4 days late
        _inv("300.00", date(2026, 5, 1), InvoiceStatus.PAID, [("300.00", date(2026, 5, 5))]),
        # open and partly paid: counts as a partial payment, not as paid
        _inv("400.00", date(2026, 10, 1), InvoiceStatus.OVERDUE, [("100.00", date(2026, 10, 3))]),
        # paid before the window: ignored entirely
        _inv("999.00", date(2025, 6, 1), InvoiceStatus.PAID, [("999.00", date(2025, 6, 1))]),
    ]
    s = summarize(invoices, since)  # type: ignore[arg-type]
    assert s.invoices_paid == 3
    assert s.on_time_rate == pytest.approx(0.333)
    assert s.avg_days_late == pytest.approx(4.7)  # (0 + 10 + 4) / 3
    assert s.partial_payment_count == 3
    assert s.total_paid == Decimal("700.00")


def test_summarize_empty() -> None:
    s = summarize([], datetime(2026, 1, 1, tzinfo=UTC))
    assert s.invoices_paid == 0
    assert s.on_time_rate is None
    assert s.avg_days_late is None
