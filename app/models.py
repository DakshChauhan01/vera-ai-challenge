"""Pydantic request/response models for the 5 required endpoints.

Context payloads themselves (category/merchant/customer/trigger) are kept as
plain ``dict`` — the testing brief defines their shape but the judge is free
to push partial / evolving payloads over the course of a test, and the
dataset shows real-world payloads already vary field-by-field (e.g. not
every merchant has ``review_themes``). Pydantic models for those would force
us to either over-constrain (reject legal partial payloads) or under-use
validation. Instead all context-field access happens through the defensive
accessors in ``app/facts.py``, which never assume a field exists.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Scope = Literal["category", "merchant", "customer", "trigger"]


# --------------------------------------------------------------------------- #
# /v1/context
# --------------------------------------------------------------------------- #
class ContextPushRequest(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


# --------------------------------------------------------------------------- #
# /v1/tick
# --------------------------------------------------------------------------- #
class TickRequest(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


class Action(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    send_as: Literal["vera", "merchant_on_behalf"]
    trigger_id: str
    template_name: str
    template_params: list[str] = Field(default_factory=list)
    body: str
    cta: str
    suppression_key: str
    rationale: str


class TickResponse(BaseModel):
    actions: list[Action] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# /v1/reply
# --------------------------------------------------------------------------- #
class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int = 1


# The reply response shape varies by `action` (send/wait/end) so we build the
# dict directly in app/main.py rather than force a single model with a pile
# of Optional fields. This model documents the union for readers/tests.
class ReplyResponseSend(BaseModel):
    action: Literal["send"] = "send"
    body: str
    cta: str
    rationale: str


class ReplyResponseWait(BaseModel):
    action: Literal["wait"] = "wait"
    wait_seconds: int
    rationale: str


class ReplyResponseEnd(BaseModel):
    action: Literal["end"] = "end"
    rationale: str


# --------------------------------------------------------------------------- #
# /v1/healthz, /v1/metadata
# --------------------------------------------------------------------------- #
class HealthzResponse(BaseModel):
    status: str
    uptime_seconds: int
    contexts_loaded: dict[str, int]


class MetadataResponse(BaseModel):
    team_name: str
    team_members: list[str]
    model: str
    approach: str
    contact_email: str
    version: str
    submitted_at: str
