"""Strict schemas for every LLM output. Nothing the model says is used unless it
validates against one of these (the ``SCHEMA_VALID`` policy rule checks this)."""

import uuid
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from payops.db.models.enums import Tone

DraftAction = Literal["reminder", "payment_plan_offer", "escalate_to_human", "no_action"]

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _LLMOutput(BaseModel):
    # Models sometimes add extra keys; they are ignored, never trusted.
    model_config = ConfigDict(extra="ignore")


class TriageItem(_LLMOutput):
    invoice_id: uuid.UUID
    priority_score: int = Field(ge=0, le=100)
    rationale: Annotated[_Text, StringConstraints(max_length=500)]


class TriageOutput(_LLMOutput):
    priorities: list[TriageItem] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [p.invoice_id for p in self.priorities]
        if len(ids) != len(set(ids)):
            raise ValueError("each invoice_id must appear once")
        return self


class ResearchNotes(_LLMOutput):
    notes: Annotated[_Text, StringConstraints(max_length=4000)]


class DraftOutput(_LLMOutput):
    """The drafting node's decision.

    For ``reminder`` and ``payment_plan_offer`` the subject and body are the customer
    message. For ``escalate_to_human`` and ``no_action`` they are an internal note.
    """

    action_type: DraftAction
    tone: Tone
    subject: Annotated[_Text, StringConstraints(max_length=200)]
    body: Annotated[_Text, StringConstraints(max_length=5000)]
    rationale: Annotated[_Text, StringConstraints(max_length=1000)]
