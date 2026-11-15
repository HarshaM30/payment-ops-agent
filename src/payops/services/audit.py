"""Audit log writer and the ``@audited`` wrapper used by every agent tool.

Each wrapped call writes exactly one ``audit_log`` row with inputs, outputs (or error),
latency, and token usage when the result carries any. The tool body runs inside a
SAVEPOINT: if it fails, its partial writes are rolled back but the audit row is kept.
"""

import functools
import inspect
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast, get_type_hints

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from payops.db.models import AgentRun, AuditLog
from payops.errors import InvalidInputError
from payops.services.pagination import decode_cursor, encode_cursor, invalid_cursor

REDACTED = "[REDACTED]"
SENSITIVE_KEYS = frozenset(
    {"api_key", "x-api-key", "x_api_key", "authorization", "password", "secret", "token"}
)


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def redact(value: Any) -> Any:
    """Recursively replace values stored under sensitive-looking keys."""
    if isinstance(value, dict):
        return {
            k: REDACTED if str(k).lower() in SENSITIVE_KEYS else redact(v) for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


def to_json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return redact(jsonable_encoder(value))


def validation_details(exc: ValidationError) -> list[dict[str, Any]]:
    """Error locations and messages only: never echo the rejected input back."""
    return [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]


def record(
    session: Session,
    *,
    event_type: str,
    name: str,
    run_id: uuid.UUID | None = None,
    actor: str | None = None,
    inputs: Any = None,
    outputs: Any = None,
    error: str | None = None,
    latency_ms: int | None = None,
    usage: TokenUsage | None = None,
) -> AuditLog:
    # An audit write must never fail. If the run id is unknown (e.g. a bad id in a tool
    # call), keep it in `inputs` but don't link the row, so the FK can't reject it.
    if run_id is not None and session.get(AgentRun, run_id) is None:
        run_id = None
    row = AuditLog(
        run_id=run_id,
        event_type=event_type,
        name=name,
        actor=actor,
        inputs=to_json(inputs) if inputs is not None else {},
        outputs=to_json(outputs) if outputs is not None else None,
        error=error,
        latency_ms=latency_ms,
        prompt_tokens=usage.prompt_tokens if usage else None,
        completion_tokens=usage.completion_tokens if usage else None,
        total_tokens=usage.total_tokens if usage else None,
    )
    session.add(row)
    session.flush()
    return row


In = TypeVar("In", bound=BaseModel)
Out = TypeVar("Out", bound=BaseModel)


class AuditedTool(Protocol[In, Out]):
    tool_name: str
    description: str
    input_model: type[In]
    output_model: type[Out]

    def __call__(
        self,
        session: Session,
        inp: In | dict[str, Any],
        *,
        run_id: uuid.UUID | None = None,
        actor: str | None = None,
    ) -> Out: ...


def audited(
    name: str, event_type: str = "tool_call"
) -> Callable[[Callable[[Session, In], Out]], AuditedTool[In, Out]]:
    """Wrap ``fn(session, input_model) -> output_model`` so every call is audited.

    The wrapper accepts either an instance of the input model or a raw dict (as an LLM
    would produce). Raw dicts are validated here, so invalid input is audited too and
    surfaces as ``InvalidInputError``.
    """

    def decorator(fn: Callable[[Session, In], Out]) -> AuditedTool[In, Out]:
        hints = get_type_hints(fn)
        params = list(inspect.signature(fn).parameters)
        input_model = cast(type[In], hints[params[1]])
        output_model = cast(type[Out], hints["return"])

        @functools.wraps(fn)
        def wrapper(
            session: Session,
            inp: In | dict[str, Any],
            *,
            run_id: uuid.UUID | None = None,
            actor: str | None = None,
        ) -> Out:
            start = time.perf_counter()

            def elapsed() -> int:
                return round((time.perf_counter() - start) * 1000)

            raw = inp if isinstance(inp, dict) else inp.model_dump(mode="json")
            try:
                model = inp if isinstance(inp, input_model) else input_model.model_validate(inp)
            except ValidationError as exc:
                details = validation_details(exc)
                record(
                    session,
                    event_type=event_type,
                    name=name,
                    run_id=run_id,
                    actor=actor,
                    inputs=raw,
                    error=f"InvalidInputError: {details}",
                    latency_ms=elapsed(),
                )
                raise InvalidInputError(f"Invalid input for tool {name}", details) from exc

            run_id = run_id or getattr(model, "run_id", None)
            try:
                with session.begin_nested():
                    result = fn(session, model)
            except Exception as exc:
                record(
                    session,
                    event_type=event_type,
                    name=name,
                    run_id=run_id,
                    actor=actor,
                    inputs=raw,
                    error=f"{type(exc).__name__}: {exc}",
                    latency_ms=elapsed(),
                )
                raise

            record(
                session,
                event_type=event_type,
                name=name,
                run_id=run_id,
                actor=actor,
                inputs=raw,
                outputs=result,
                latency_ms=elapsed(),
                usage=getattr(result, "usage", None),
            )
            return result

        tool = cast(AuditedTool[In, Out], wrapper)
        tool.tool_name = name
        tool.description = inspect.cleandoc(fn.__doc__ or "")
        tool.input_model = input_model
        tool.output_model = output_model
        return tool

    return decorator


def list_entries(
    session: Session,
    *,
    run_id: uuid.UUID | None,
    event_type: str | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[AuditLog], str | None]:
    """Newest first, keyset-paginated on id."""
    stmt = select(AuditLog)
    if run_id is not None:
        stmt = stmt.where(AuditLog.run_id == run_id)
    if event_type is not None:
        stmt = stmt.where(AuditLog.event_type == event_type)
    if cursor:
        before = decode_cursor(cursor, {"id"})["id"]
        if not isinstance(before, int) or isinstance(before, bool):
            raise invalid_cursor()
        stmt = stmt.where(AuditLog.id < before)
    rows = list(session.scalars(stmt.order_by(AuditLog.id.desc()).limit(limit + 1)))
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor({"id": rows[-1].id})
    return rows, next_cursor
