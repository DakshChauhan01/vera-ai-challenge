"""In-memory, versioned, idempotent context store + suppression store +
conversation store.

Everything here is process-local memory, cleared only by /v1/teardown or a
process restart (which the harness promises won't happen mid-test). No
external database is used, per the privacy constraint: nothing ever leaves
the process except to an LLM API (and this build never calls one at all).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import MERCHANT_OPT_OUT_SUPPRESS_SECONDS

SCOPES = ("category", "merchant", "customer", "trigger")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )


def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Best-effort ISO-8601 parser. Returns None rather than raising, since
    context payloads are judge-controlled input we must never crash on."""
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


@dataclass
class ContextEntry:
    version: int
    payload: dict[str, Any]
    delivered_at: str
    stored_at: str


class ContextStore:
    """Keyed by (scope, context_id) -> ContextEntry. Thread-safe enough for
    an ASGI app under normal (single-worker, async) operation; a lock guards
    the read-modify-write on version comparison."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], ContextEntry] = {}
        self._lock = threading.Lock()

    def push(
        self, scope: str, context_id: str, version: int, payload: dict[str, Any], delivered_at: str
    ) -> tuple[bool, dict[str, Any]]:
        key = (scope, context_id)
        stored_at = utcnow_iso()
        with self._lock:
            cur = self._data.get(key)
            if cur is not None and version < cur.version:
                return False, {"accepted": False, "reason": "stale_version", "current_version": cur.version}
            if cur is not None and version == cur.version:
                # Idempotent no-op re-post of the same version.
                return True, {"accepted": True, "ack_id": f"ack_{context_id}_v{version}", "stored_at": cur.stored_at}
            self._data[key] = ContextEntry(version=version, payload=payload, delivered_at=delivered_at, stored_at=stored_at)
            return True, {"accepted": True, "ack_id": f"ack_{context_id}_v{version}", "stored_at": stored_at}

    def get(self, scope: str, context_id: str) -> Optional[dict[str, Any]]:
        entry = self._data.get((scope, context_id))
        return entry.payload if entry else None

    def get_version(self, scope: str, context_id: str) -> Optional[int]:
        entry = self._data.get((scope, context_id))
        return entry.version if entry else None

    def ids(self, scope: str) -> list[str]:
        return [cid for (s, cid) in self._data.keys() if s == scope]

    def all_payloads(self, scope: str) -> dict[str, dict[str, Any]]:
        return {cid: entry.payload for (s, cid), entry in self._data.items() if s == scope}

    def counts(self) -> dict[str, int]:
        counts = {s: 0 for s in SCOPES}
        for (scope, _cid) in self._data.keys():
            counts[scope] = counts.get(scope, 0) + 1
        return counts

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class SuppressionStore:
    """suppression_key -> expires_at (epoch seconds). Also tracks merchant-
    level opt-outs separately, since those apply across *every* trigger and
    suppression_key for that merchant, not just one."""

    def __init__(self) -> None:
        self._keys: dict[str, float] = {}
        self._merchant_opt_out: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_active(self, suppression_key: str, now_epoch: float) -> bool:
        if not suppression_key:
            return False
        expires = self._keys.get(suppression_key)
        return expires is not None and expires > now_epoch

    def suppress(self, suppression_key: str, expires_at_iso: Optional[str], now_epoch: float) -> None:
        if not suppression_key:
            return
        dt = parse_iso(expires_at_iso)
        expires_epoch = dt.timestamp() if dt else (now_epoch + 7 * 24 * 3600)
        with self._lock:
            # Keep the furthest-out expiry if re-suppressed.
            self._keys[suppression_key] = max(expires_epoch, self._keys.get(suppression_key, 0))

    def opt_out_merchant(self, merchant_id: str, now_epoch: float) -> None:
        with self._lock:
            self._merchant_opt_out[merchant_id] = now_epoch + MERCHANT_OPT_OUT_SUPPRESS_SECONDS

    def is_merchant_opted_out(self, merchant_id: str, now_epoch: float) -> bool:
        expires = self._merchant_opt_out.get(merchant_id)
        return expires is not None and expires > now_epoch

    def clear(self) -> None:
        with self._lock:
            self._keys.clear()
            self._merchant_opt_out.clear()


@dataclass
class Turn:
    role: str  # "vera" | "merchant" | "customer"
    message: str
    ts: float
    turn_number: int


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str]
    send_as: str
    trigger_id: str
    trigger_kind: str
    category_slug: str
    status: str = "active"  # active | waiting | ended
    wait_until_epoch: float = 0.0
    turns: list[Turn] = field(default_factory=list)
    sent_bodies: list[str] = field(default_factory=list)
    identical_incoming_streak: int = 0
    last_incoming_normalized: str = ""
    autoreply_nudged: bool = False
    facts_used: list[str] = field(default_factory=list)
    opened_at: float = field(default_factory=time.time)

    def record_send(self, body: str, turn_number: int) -> None:
        self.turns.append(Turn(role=self.send_as, message=body, ts=time.time(), turn_number=turn_number))
        self.sent_bodies.append(body)

    def record_incoming(self, message: str, turn_number: int) -> None:
        self.turns.append(Turn(role="merchant", message=message, ts=time.time(), turn_number=turn_number))


class ConversationStore:
    def __init__(self) -> None:
        self._data: dict[str, ConversationState] = {}
        self._merchant_last_touch: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self, state: ConversationState) -> None:
        with self._lock:
            self._data[state.conversation_id] = state
            self._merchant_last_touch[state.merchant_id] = time.time()

    def get(self, conversation_id: str) -> Optional[ConversationState]:
        return self._data.get(conversation_id)

    def touch_merchant(self, merchant_id: str) -> None:
        self._merchant_last_touch[merchant_id] = time.time()

    def last_touch_epoch(self, merchant_id: str) -> Optional[float]:
        return self._merchant_last_touch.get(merchant_id)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._merchant_last_touch.clear()
