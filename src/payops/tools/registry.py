"""Tool registry: name -> audited function, plus JSON schemas exported for the LLM."""

import copy
import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from payops.errors import InvalidInputError
from payops.services import audit
from payops.services.audit import AuditedTool
from payops.tools.actions import draft_message, flag_for_review, schedule_followup
from payops.tools.customers import get_customer
from payops.tools.history import get_payment_history
from payops.tools.invoices import get_invoices

TOOLS: dict[str, AuditedTool[Any, Any]] = {
    t.tool_name: t
    for t in (
        get_customer,
        get_invoices,
        get_payment_history,
        draft_message,
        schedule_followup,
        flag_for_review,
    )
}
READ_ONLY_TOOLS = frozenset({"get_customer", "get_invoices", "get_payment_history"})

# Filled in by the runtime, never by the model.
INJECTED_FIELDS = frozenset({"run_id"})


def _parameters(model: type[BaseModel]) -> dict[str, Any]:
    schema = copy.deepcopy(model.model_json_schema())
    for field in INJECTED_FIELDS:
        schema.get("properties", {}).pop(field, None)
        if field in schema.get("required", []):
            schema["required"].remove(field)
    schema.pop("title", None)
    return schema


def tool_schemas(names: frozenset[str] | None = None) -> list[dict[str, Any]]:
    """OpenAI-style function definitions (the format LiteLLM accepts for every provider)."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": tool.description,
                "parameters": _parameters(tool.input_model),
            },
        }
        for name, tool in TOOLS.items()
        if names is None or name in names
    ]


def call_tool(
    session: Session,
    name: str,
    args: dict[str, Any],
    *,
    run_id: uuid.UUID,
    actor: str = "agent",
) -> BaseModel:
    """Dispatch an LLM tool call. Injected fields always come from the runtime."""
    tool = TOOLS.get(name)
    if tool is None:
        audit.record(
            session,
            event_type="tool_call",
            name=name[:100],
            run_id=run_id,
            actor=actor,
            inputs=args,
            error=f"InvalidInputError: unknown tool {name!r}",
        )
        raise InvalidInputError(f"Unknown tool {name!r}")
    merged = {k: v for k, v in args.items() if k not in INJECTED_FIELDS}
    if "run_id" in tool.input_model.model_fields:
        merged["run_id"] = run_id
    return tool(session, merged, run_id=run_id, actor=actor)
