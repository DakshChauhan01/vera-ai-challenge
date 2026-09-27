"""Decision/ranking engine for POST /v1/tick.

Pipeline (matches the build spec's step 3):
  1. For each available trigger, resolve its merchant + category (+ customer
     if scoped to one). Drop anything we can't resolve.
  2. Score how well the trigger matches the merchant's current state.
  3. Filter out anything whose suppression_key is active, or whose merchant
     has opted out, or that was just messaged very recently and hasn't
     replied yet (avoid double-texting within one session window).
  4. Sort by score, enforce one action per distinct engagement target
     (merchant for merchant-scope triggers, merchant+customer for
     customer-scope triggers) per tick, cap at MAX_ACTIONS_PER_TICK.
  5. For each selected trigger: extract facts -> compose -> validate ->
     (fallback if validation fails) -> build the Action and register
     conversation + suppression state.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Optional

from app import composer, config, validator
from app.facts import Facts, days_until, extract, find_signal, g
from app.language import resolve_language
from app.store import ConversationState, ConversationStore, ContextStore, SuppressionStore, parse_iso

SIGNAL_HINTS: dict[str, list[str]] = {
    "perf_dip": ["ctr_below_peer", "perf_dip"],
    "seasonal_perf_dip": ["seasonal_dip"],
    "perf_spike": ["above_peer", "growing_views", "perf_spike", "above_peer_ctr"],
    "dormant_with_vera": ["dormant_with_vera"],
    "renewal_due": ["renewal_due_soon"],
    "winback_eligible": ["winback_eligible"],
    "gbp_unverified": ["unverified_gbp"],
    "competitor_opened": ["competitor"],
    "curious_ask_due": ["engaged_in_last"],
}


def _resolve_now(now_str: str) -> datetime:
    dt = parse_iso(now_str)
    return dt or datetime.now(timezone.utc)


def _score(trigger: dict, merchant: dict, now_dt: datetime, recently_touched: bool) -> float:
    urgency = trigger.get("urgency", 1)
    try:
        urgency = float(urgency)
    except (TypeError, ValueError):
        urgency = 1.0
    score = urgency * 10.0

    kind = trigger.get("kind", "")
    hints = SIGNAL_HINTS.get(kind, [])
    signals = merchant.get("signals") or []
    if hints and any(find_signal(merchant, h) for h in hints):
        score += 8.0

    days_left = days_until(trigger.get("expires_at"), now_dt)
    if days_left is not None and days_left <= 3:
        score += (4 - max(days_left, 0)) * 2.0

    if trigger.get("source") == "external":
        score += 1.0

    if recently_touched:
        score -= 60.0  # strong deprioritization, overridable only by very high urgency

    return score


def _engagement_key(trigger: dict) -> str:
    merchant_id = trigger.get("merchant_id", "")
    if trigger.get("scope") == "customer" and trigger.get("customer_id"):
        return f"{merchant_id}::{trigger['customer_id']}"
    return merchant_id


def _short(identifier: str, max_len: int = 24) -> str:
    return (identifier or "x")[:max_len]


def _build_conversation_id(merchant_id: str, kind: str, trigger_id: str) -> str:
    """Decodable-but-guaranteed-unique: readable merchant+kind prefix (like
    the brief's own `conv_priya_recall_2026_11` examples) plus a short
    deterministic hash of the full trigger_id, so two different trigger IDs
    that happen to share a human-readable tail never collide, regardless of
    the judge's own trigger-id naming scheme."""
    parts = merchant_id.split("_")
    merchant_tail = parts[1] if len(parts) > 1 else merchant_id
    digest = hashlib.sha1(trigger_id.encode("utf-8")).hexdigest()[:8]
    return f"conv_{_short(merchant_tail, 20)}_{kind}_{digest}"


class DecisionEngine:
    def __init__(self, contexts: ContextStore, suppressions: SuppressionStore, conversations: ConversationStore):
        self.contexts = contexts
        self.suppressions = suppressions
        self.conversations = conversations

    def run_tick(self, now_str: str, available_triggers: list[str], deadline_epoch: float) -> list[dict]:
        now_dt = _resolve_now(now_str)
        now_epoch = time.time()

        candidates = []
        for trigger_id in available_triggers:
            if time.time() > deadline_epoch:
                break
            trigger = self.contexts.get("trigger", trigger_id)
            if not trigger:
                continue

            merchant_id = trigger.get("merchant_id")
            merchant = self.contexts.get("merchant", merchant_id) if merchant_id else None
            if not merchant:
                continue

            if self.suppressions.is_merchant_opted_out(merchant_id, now_epoch):
                continue

            suppression_key = trigger.get("suppression_key", "")
            if suppression_key and self.suppressions.is_active(suppression_key, now_epoch):
                continue

            category_slug = merchant.get("category_slug")
            category = self.contexts.get("category", category_slug) if category_slug else None
            if not category:
                continue

            customer = None
            if trigger.get("scope") == "customer":
                customer_id = trigger.get("customer_id")
                if not customer_id:
                    continue
                customer = self.contexts.get("customer", customer_id)
                if not customer:
                    # Customer context hasn't arrived yet — don't fabricate
                    # identity/relationship detail; wait for a later tick.
                    continue

            last_touch = self.conversations.last_touch_epoch(merchant_id)
            recently_touched = bool(
                last_touch and (now_epoch - last_touch) < config.RECENT_TOUCH_SOFT_COOLDOWN_SECONDS
            )
            urgency = trigger.get("urgency", 1)
            if recently_touched and isinstance(urgency, (int, float)) and urgency >= 5:
                recently_touched = False  # critical alerts override the cooldown

            score = _score(trigger, merchant, now_dt, recently_touched)
            if recently_touched and score < 0:
                continue  # still too soon and not urgent enough to override

            kind = trigger.get("kind", "")
            facts = extract(kind, category, merchant, trigger, customer, now_dt)
            if not facts.ok:
                score -= 15.0  # weak/placeholder payload: still eligible, but deprioritized

            candidates.append(
                {
                    "trigger": trigger,
                    "trigger_id": trigger_id,
                    "merchant": merchant,
                    "merchant_id": merchant_id,
                    "category": category,
                    "customer": customer,
                    "facts": facts,
                    "score": score,
                    "kind": kind,
                    "engagement_key": _engagement_key(trigger),
                }
            )

        candidates.sort(key=lambda c: c["score"], reverse=True)

        actions: list[dict] = []
        used_keys: set[str] = set()

        for cand in candidates:
            if time.time() > deadline_epoch:
                break
            if len(actions) >= config.MAX_ACTIONS_PER_TICK:
                break
            if cand["engagement_key"] in used_keys:
                continue

            action = self._compose_action(cand, now_epoch)
            if action is None:
                continue
            actions.append(action)
            used_keys.add(cand["engagement_key"])

        return actions

    def _compose_action(self, cand: dict, now_epoch: float) -> Optional[dict]:
        trigger = cand["trigger"]
        merchant = cand["merchant"]
        category = cand["category"]
        customer = cand["customer"]
        facts: Facts = cand["facts"]
        kind = cand["kind"]

        lang = resolve_language(merchant, customer)
        # If the extractor couldn't find enough real detail for a bespoke
        # kind-specific template (e.g. a placeholder-only payload), force
        # the generic renderer rather than let a bespoke template run with
        # missing fields — the generic path only ever prints facts it
        # actually found (or falls back to a merchant signal), never blanks.
        compose_kind = kind if facts.is_specific else "__generic__"
        body, cta = composer.compose(compose_kind, category, merchant, trigger, customer, facts, lang)
        ok, reason = validator.validate(body, category, merchant, trigger, customer, facts)
        rationale_suffix = ""
        if not ok:
            body, cta = validator.safe_fallback(category, merchant, trigger, customer, lang)
            rationale_suffix = f" (fell back to safe template: {reason})"

        send_as = "merchant_on_behalf" if customer is not None else "vera"
        suppression_key = trigger.get("suppression_key") or f"{kind}:{cand['merchant_id']}:{cand['trigger_id']}"
        conversation_id = _build_conversation_id(cand["merchant_id"], kind, cand["trigger_id"])

        rationale = f"{facts.why_now or kind.replace('_', ' ')}; matched to {'this customer' if customer else 'this merchant'}'s actual context.{rationale_suffix}"

        state = ConversationState(
            conversation_id=conversation_id,
            merchant_id=cand["merchant_id"],
            customer_id=g(customer, "customer_id") if customer else None,
            send_as=send_as,
            trigger_id=cand["trigger_id"],
            trigger_kind=kind,
            category_slug=merchant.get("category_slug", ""),
            facts_used=list(facts.values.keys()),
        )
        state.record_send(body, turn_number=1)
        self.conversations.create(state)
        self.suppressions.suppress(suppression_key, trigger.get("expires_at"), now_epoch)

        template_params = [
            g(merchant, "identity", "name") or cand["merchant_id"],
            facts.why_now or kind,
            body,
        ]

        return {
            "conversation_id": conversation_id,
            "merchant_id": cand["merchant_id"],
            "customer_id": g(customer, "customer_id") if customer else None,
            "send_as": send_as,
            "trigger_id": cand["trigger_id"],
            "template_name": f"vera_{kind}_v1" if send_as == "vera" else f"merchant_{kind}_v1",
            "template_params": template_params,
            "body": body,
            "cta": cta,
            "suppression_key": suppression_key,
            "rationale": rationale,
        }
