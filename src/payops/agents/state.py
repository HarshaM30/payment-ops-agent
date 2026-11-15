"""Graph state and the per-run context the nodes share."""

import json
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, TypedDict

from sqlalchemy.orm import Session

from payops.agents.facts import InvoiceFacts
from payops.config import Settings
from payops.llm.client import LLMCallRecord, LLMClient
from payops.schemas.llm import DraftOutput
from payops.services import audit

PROMPTS_DIR = Path(__file__).parent / "prompts"
NODES_WITH_PROMPTS = ("triage", "research", "drafting")


@lru_cache
def load_prompt(node: str, version: str) -> str:
    path = PROMPTS_DIR / f"{node}_{version}.md"
    if not path.is_file():
        raise ValueError(f"Prompt {path.name} not found for version {version!r}")
    return path.read_text(encoding="utf-8").strip()


class AgentState(TypedDict, total=False):
    run_id: uuid.UUID
    queue: list[uuid.UUID]  # invoice ids, highest priority first
    index: int  # position in `queue` of the invoice being worked on
    scores: dict[str, dict[str, Any]]
    # Per-invoice working state, reset by finalize.
    research_notes: str | None
    research_ok: bool
    draft: DraftOutput | None
    draft_errors: list[dict[str, Any]] | None
    revisions: int
    feedback: str | None
    final: dict[str, Any] | None
    # Run summary.
    outcomes: list[dict[str, Any]]


@dataclass
class AgentContext:
    session: Session
    llm: LLMClient
    settings: Settings
    run_id: uuid.UUID
    prompt_version: str
    facts: dict[uuid.UUID, InvoiceFacts] = field(default_factory=dict)

    def prompt(self, node: str) -> str:
        return load_prompt(node, self.prompt_version)

    def record_llm_call(self, call: LLMCallRecord) -> None:
        """Recorder passed to the LLM client: one audit row per provider call."""
        response = call.response
        audit.record(
            self.session,
            event_type="llm_call",
            name=call.node,
            run_id=self.run_id,
            actor=f"{self.llm.provider}/{self.llm.model}",
            inputs={
                "purpose": call.purpose,
                "messages": call.request.messages,
                "tools": [t["function"]["name"] for t in call.request.tools or []],
                "json_mode": call.request.json_mode,
            },
            outputs=(
                {
                    "content": response.content,
                    "tool_calls": [
                        {"id": c.id, "name": c.name, "arguments": c.arguments}
                        for c in response.tool_calls
                    ],
                    "attempts": response.attempts,
                }
                if response
                else None
            ),
            error=call.error,
            latency_ms=call.latency_ms,
            usage=call.usage,
        )

    def record_decision(
        self,
        node: str,
        *,
        inputs: Any = None,
        outputs: Any = None,
        error: str | None = None,
        event_type: str = "node_decision",
        started: float | None = None,
    ) -> None:
        """``started`` is a ``time.perf_counter()`` value; latency is measured from it."""
        latency = round((time.perf_counter() - started) * 1000) if started is not None else None
        audit.record(
            self.session,
            event_type=event_type,
            name=node,
            run_id=self.run_id,
            actor="agent",
            inputs=inputs,
            outputs=outputs,
            error=error,
            latency_ms=latency,
        )


def user_json(data: Any) -> dict[str, str]:
    return {"role": "user", "content": json.dumps(data, indent=2, default=str)}


def current_invoice(state: AgentState) -> uuid.UUID:
    return state["queue"][state["index"]]
