"""Research: an LLM tool-calling loop over the read-only tools, capped at N steps."""

import json
import time

from pydantic import ValidationError

from payops.agents.state import AgentContext, AgentState, current_invoice, user_json
from payops.errors import AppError
from payops.llm.client import LLMRequest, ToolCall
from payops.schemas.llm import ResearchNotes
from payops.services import audit
from payops.tools.registry import READ_ONLY_TOOLS, call_tool, tool_schemas

STEP_LIMIT_NOTE = "Research stopped at the step limit before the model finished."


def _run_tool(ctx: AgentContext, call: ToolCall) -> str:
    """Execute one tool call and return the tool message content (result or error)."""
    if call.name not in READ_ONLY_TOOLS:
        audit.record(
            ctx.session,
            event_type="tool_call",
            name=call.name[:100],
            run_id=ctx.run_id,
            actor="agent",
            inputs={"arguments": call.arguments},
            error="PermissionError: only read-only tools are allowed during research",
        )
        return json.dumps({"error": f"Tool {call.name!r} is not available during research"})
    try:
        args = call.parsed_arguments()
    except ValueError as exc:
        audit.record(
            ctx.session,
            event_type="tool_call",
            name=call.name,
            run_id=ctx.run_id,
            actor="agent",
            inputs={"arguments": call.arguments},
            error=f"InvalidInputError: arguments are not a JSON object ({exc})",
        )
        return json.dumps({"error": "Arguments must be a JSON object"})
    try:
        result = call_tool(ctx.session, call.name, args, run_id=ctx.run_id)
    except AppError as exc:  # already audited by the tool wrapper
        return json.dumps({"error": exc.message, "details": exc.details}, default=str)
    return result.model_dump_json()


def research(state: AgentState, ctx: AgentContext) -> AgentState:
    started = time.perf_counter()
    invoice_id = current_invoice(state)
    facts = ctx.facts[invoice_id]
    messages: list[dict] = [
        {"role": "system", "content": ctx.prompt("research")},
        user_json(
            {
                "invoice_id": str(facts.invoice_id),
                "invoice_number": facts.invoice_number,
                "customer_id": str(facts.customer_id),
                "status": facts.status.value,
                "balance": str(facts.balance),
                "days_overdue": facts.days_overdue,
            }
        ),
    ]
    tools = tool_schemas(READ_ONLY_TOOLS)
    context = {"invoice_number": facts.invoice_number, "customer_id": str(facts.customer_id)}
    notes: str | None = None
    ok = True
    tools_called: list[str] = []
    steps = 0

    for steps in range(1, ctx.settings.research_max_steps + 1):  # noqa: B007
        response = ctx.llm.complete(
            LLMRequest("research", list(messages), tools, context=context),
            recorder=ctx.record_llm_call,
        )
        if not response.tool_calls:
            try:
                notes = ResearchNotes(notes=response.content or "").notes
            except ValidationError:
                ok = False
            break
        messages.append(
            {
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.arguments},
                    }
                    for c in response.tool_calls
                ],
            }
        )
        for call in response.tool_calls:
            tools_called.append(call.name)
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": _run_tool(ctx, call)}
            )
    else:
        notes = STEP_LIMIT_NOTE

    ctx.record_decision(
        "research",
        inputs={"invoice_id": invoice_id, "max_steps": ctx.settings.research_max_steps},
        outputs={"steps": steps, "tools_called": tools_called, "notes": notes, "schema_ok": ok},
        error=None if ok else "ResearchNotes failed validation (empty response)",
        started=started,
    )
    return {
        "research_notes": notes,
        "research_ok": ok,
        "revisions": 0,
        "feedback": None,
        "draft": None,
        "draft_errors": None,
        "final": None,
    }
