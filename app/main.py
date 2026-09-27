"""Vera 2.0 — FastAPI wiring for the 5 required endpoints (+ optional
teardown). All business logic lives in app/decision_engine.py and
app/conversation.py; this module is deliberately thin: parse, delegate,
never let an unhandled exception escape as anything other than well-formed
JSON with a 200/400/409, since a malformed response is scored as harshly as
a wrong one (testing-brief §10).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app import config
from app.conversation import ConversationManager
from app.decision_engine import DecisionEngine
from app.models import HealthzResponse, MetadataResponse, ReplyRequest, TickRequest
from app.store import ContextStore, ConversationStore, SuppressionStore, utcnow_iso

APP_START = time.time()

app = FastAPI(title="Vera 2.0 — Deterministic Merchant Message Engine", version="1.0.0")

contexts = ContextStore()
suppressions = SuppressionStore()
conversations = ConversationStore()

decision_engine = DecisionEngine(contexts, suppressions, conversations)
conversation_manager = ConversationManager(contexts, suppressions, conversations)

VALID_SCOPES = {"category", "merchant", "customer", "trigger"}

# `from_role` is a deliberate closed set, not free text. Nothing in
# conversation.py currently branches on it (the reply-classification patterns
# — opt-out, hostile, deferral, etc. — apply the same way regardless of who
# sent the message), but the field exists to name *whose* message this is,
# and only two participants exist in this system's model of a conversation:
# the merchant and the merchant's customer. A value outside that set is not
# "some other kind of participant we support" — it is a caller bug or a
# malformed/adversarial payload, so it is rejected the same way /v1/context
# rejects an invalid `scope`, rather than being silently accepted and passed
# through to a state machine that assigns it no meaning at all.
VALID_FROM_ROLES = {"merchant", "customer"}


def _reply_error(reason: str, details: str, status_code: int = 400) -> JSONResponse:
    """Same envelope shape /v1/context uses for its validation failures
    ({"accepted": false, "reason", "details"}), so every 4xx this service
    returns for a malformed request looks the same regardless of endpoint —
    never FastAPI's raw {"detail": [...]} 422 shape."""
    return JSONResponse(status_code=status_code, content={"accepted": False, "reason": reason, "details": details})


@app.get("/")
async def root():
    return {
        "service": "Vera 2.0 — Deterministic Merchant Message Engine",
        "endpoints": ["/v1/healthz", "/v1/metadata", "/v1/context", "/v1/tick", "/v1/reply", "/v1/teardown"],
    }


@app.get("/v1/healthz")
async def healthz():
    return HealthzResponse(
        status="ok",
        uptime_seconds=int(time.time() - APP_START),
        contexts_loaded=contexts.counts(),
    )


@app.get("/v1/metadata")
async def metadata():
    return MetadataResponse(
        team_name="Vera 2.0",
        team_members=["Daksh Chauhan"],
        model="deterministic-template-engine (no LLM in the composition path)",
        approach=(
            "Rule-based decision/ranking layer scores every available trigger against the merchant's "
            "live context and picks the single strongest signal; a parameterized template layer (keyed "
            "by category x trigger-kind) phrases only the facts the decision layer selected; an "
            "anti-hallucination validator checks every number/date in the output against the source "
            "context before it is sent, falling back to a guaranteed-safe template otherwise."
        ),
        contact_email="chauhan.dakshdc1@gmail.com",
        version="1.0.0",
        submitted_at=utcnow_iso(),
    )


@app.post("/v1/context")
async def push_context(request: Request):
    raw = await request.body()
    if len(raw) > config.CONTEXT_PAYLOAD_CAP_BYTES:
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "payload_too_large", "details": f"payload exceeds {config.CONTEXT_PAYLOAD_CAP_BYTES} bytes"},
        )

    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "malformed_json", "details": str(exc)})

    scope = body.get("scope")
    context_id = body.get("context_id")
    version = body.get("version")
    payload = body.get("payload")
    delivered_at = body.get("delivered_at", utcnow_iso())

    if scope not in VALID_SCOPES:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {sorted(VALID_SCOPES)}"})
    if not context_id or not isinstance(context_id, str):
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_context_id", "details": "context_id is required"})
    if not isinstance(version, int):
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_version", "details": "version must be an integer"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_payload", "details": "payload must be an object"})

    accepted, response = contexts.push(scope, context_id, version, payload, delivered_at)
    status_code = 200 if accepted else 409
    return JSONResponse(status_code=status_code, content=response)


@app.post("/v1/tick")
async def tick(body: TickRequest):
    start = time.time()
    deadline = start + config.TICK_SOFT_BUDGET_SECONDS
    try:
        actions = decision_engine.run_tick(body.now, body.available_triggers, deadline)
    except Exception:  # noqa: BLE001 — never let tick 500; restraint beats a crash.
        return {"actions": []}
    if time.time() > start + config.TICK_SOFT_BUDGET_SECONDS + 2:
        # Blew well past budget somehow — better to return nothing than late.
        return {"actions": []}
    return {"actions": actions}


@app.post("/v1/reply")
async def reply(request: Request):
    # Parsed manually (like /v1/context) rather than via a typed `body:
    # ReplyRequest` parameter, so that a malformed request never reaches
    # FastAPI's own validation layer and comes back as its raw
    # {"detail": [...]} 422 shape. Every validation failure here returns the
    # same {"accepted": false, "reason", "details"} envelope /v1/context uses.
    raw = await request.body()
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return _reply_error("malformed_json", str(exc))

    if not isinstance(body, dict):
        return _reply_error("invalid_payload", "request body must be a JSON object")

    conversation_id = body.get("conversation_id")
    from_role = body.get("from_role")
    message = body.get("message")
    received_at = body.get("received_at")

    if not conversation_id or not isinstance(conversation_id, str):
        return _reply_error("invalid_conversation_id", "conversation_id is required and must be a string")
    if from_role not in VALID_FROM_ROLES:
        return _reply_error("invalid_from_role", f"from_role must be one of {sorted(VALID_FROM_ROLES)}, got {from_role!r}")
    if not isinstance(message, str):
        return _reply_error("missing_message", "message is required and must be a string")
    if not received_at or not isinstance(received_at, str):
        return _reply_error("invalid_received_at", "received_at is required and must be a string")

    # Everything explicitly spec'd above has been checked by hand with a
    # specific reason code; ReplyRequest still catches anything left over
    # (e.g. turn_number sent as a non-integer) under the same envelope.
    try:
        parsed = ReplyRequest(**body)
    except ValidationError as exc:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return _reply_error("invalid_payload", errors)

    try:
        result = conversation_manager.handle_reply(parsed)
    except Exception:  # noqa: BLE001
        result = {"action": "wait", "wait_seconds": config.DEFAULT_WAIT_SECONDS, "rationale": "Internal hiccup handling this reply; backing off rather than sending something broken."}
    return result


@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    suppressions.clear()
    conversations.clear()
    return {"status": "ok"}
