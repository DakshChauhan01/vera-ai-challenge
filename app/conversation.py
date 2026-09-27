"""Conversation-state manager for POST /v1/reply.

Classifies the incoming message into one of: auto-reply/canned, hostile /
opt-out, deferral, explicit action-intent, off-topic curveball, or a plain
continuation — and returns exactly one of send / wait / end, per the testing
brief's 3-way contract. Every "send" body is checked against everything
already sent in this conversation so we never repeat a body verbatim
(operational penalty per testing-brief §10), and always re-reads live
context (merchant/category/trigger) at reply time rather than relying on
whatever was true when the conversation started.
"""

from __future__ import annotations

import re
import time
from typing import Optional

from app import config
from app.facts import extract, g
from app.language import merchant_first_name, resolve_language
from app.store import ConversationState, ConversationStore, ContextStore, SuppressionStore, parse_iso

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")

CANNED_PHRASES = [
    "thank you for contacting",
    "thanks for contacting",
    "will respond shortly",
    "will get back to you",
    "we have received your message",
    "automated response",
    "out of office",
    "this is an automated",
    "team will respond",
    "will reply soon",
]

OPT_OUT_PATTERNS = [
    "stop messaging", "stop sending", "unsubscribe", "don't message", "dont message",
    "don't contact", "dont contact", "remove me", "opt out", "opt-out",
    "not interested", "no thanks", "please stop",
]

HOSTILE_PATTERNS = [
    "useless", "spam", "idiot", "stupid", "shut up", "nonsense", "waste of time",
    "annoying", "harass", "bothering me", "leave me alone", "rubbish",
]

DEFERRAL_PATTERNS = [
    "give me time", "not now", "later", "remind me", "hold on", "wait",
    "let me think", "call you back", "get back to you", "busy right now",
]

ACTION_INTENT_PATTERNS = [
    "let's do it", "lets do it", "let's do this", "lets do this", "go ahead", "go for it",
    "sounds good let's", "i want to join", "sign me up", "proceed", "confirm",
    "let's start", "lets start", "do it", "yes let's", "yes lets",
]

QUALIFYING_PATTERNS = [
    "would you", "do you think", "can you tell me", "what if", "how about",
    "would that work", "which one should", "should i",
]

OFFTOPIC_PATTERNS = [
    "by the way", "btw", "can you also help", "unrelated question", "off topic",
    "can you help me with my", "unrelated to this",
    # common real-world curveball domains that have nothing to do with
    # merchant/customer engagement messaging — a deliberately explicit list
    # so a genuine follow-up question about the trigger itself (which won't
    # match any of these) is never wrongly deflected.
    "gst filing", "income tax", "file my taxes", "get a loan", "insurance policy",
    "visa", "passport", "chartered accountant", "hire a lawyer", "legal advice",
]

_NEXT_STEP_LABEL = {
    "research_digest": "pulling the abstract and drafting the patient-ed WhatsApp",
    "regulation_change": "drafting the SOP note",
    "recall_due": "confirming the slot",
    "perf_dip": "starting the win-back offer",
    "perf_spike": "scheduling the follow-up post",
    "seasonal_perf_dip": "drafting the retention nudge",
    "renewal_due": "processing the renewal",
    "milestone_reached": "scheduling the milestone post",
    "review_theme_emerged": "sending the response template to your team",
    "dormant_with_vera": "picking this back up",
    "curious_ask_due": "turning your answer into a post",
    "active_planning_intent": "finalising the plan",
    "winback_eligible": "flagging your account manager",
    "gbp_unverified": "starting Google verification",
    "cde_opportunity": "blocking the calendar slot",
    "competitor_opened": "updating your GBP description",
    "festival_upcoming": "drafting the festival post",
    "chronic_refill_due": "dispatching the refill",
    "trial_followup": "booking the next session",
    "appointment_tomorrow": "confirming tomorrow's slot",
    "wedding_package_followup": "holding your slot",
    "customer_lapsed_soft": "holding a spot for you",
    "customer_lapsed_hard": "holding a spot for you",
    "supply_alert": "drafting the customer note and pickup workflow",
}


def _normalize(msg: str) -> str:
    s = msg.strip().lower()
    s = _PUNCT_RE.sub("", s)
    s = _WS_RE.sub(" ", s)
    return s


def _contains_any(text: str, patterns: list[str]) -> bool:
    return any(p in text for p in patterns)


def _looks_canned(text: str) -> bool:
    return _contains_any(text, CANNED_PHRASES)


class ConversationManager:
    def __init__(self, contexts: ContextStore, suppressions: SuppressionStore, conversations: ConversationStore):
        self.contexts = contexts
        self.suppressions = suppressions
        self.conversations = conversations

    # ------------------------------------------------------------------ #
    def _get_or_create(self, req) -> ConversationState:
        conv = self.conversations.get(req.conversation_id)
        if conv is not None:
            return conv
        merchant_id = req.merchant_id or ""
        merchant = self.contexts.get("merchant", merchant_id) if merchant_id else None
        category_slug = merchant.get("category_slug", "") if merchant else ""
        conv = ConversationState(
            conversation_id=req.conversation_id,
            merchant_id=merchant_id,
            customer_id=req.customer_id,
            send_as="merchant_on_behalf" if req.customer_id else "vera",
            trigger_id="",
            trigger_kind="unknown",
            category_slug=category_slug,
        )
        self.conversations.create(conv)
        return conv

    def _resolve_context(self, conv: ConversationState):
        merchant = self.contexts.get("merchant", conv.merchant_id) or {}
        category = self.contexts.get("category", conv.category_slug or merchant.get("category_slug", "")) or {}
        trigger = self.contexts.get("trigger", conv.trigger_id) or {}
        customer = self.contexts.get("customer", conv.customer_id) if conv.customer_id else None
        return merchant, category, trigger, customer

    def _dedupe(self, conv: ConversationState, options: list[str]) -> str:
        for opt in options:
            if opt and opt not in conv.sent_bodies:
                return opt
        base = options[-1] if options else "Following up on this — still keen to help."
        return base + " (circling back once more on this)"

    def _next_step_label(self, kind: str) -> str:
        return _NEXT_STEP_LABEL.get(kind, "moving ahead on this")

    # ------------------------------------------------------------------ #
    def handle_reply(self, req) -> dict:
        conv = self._get_or_create(req)
        merchant, category, trigger, customer = self._resolve_context(conv)
        lang = resolve_language(merchant, customer) if merchant else "en"
        name = merchant_first_name(merchant) if merchant else "there"
        label = self._next_step_label(conv.trigger_kind)

        message = req.message or ""
        normalized = _normalize(message)
        text = message.lower()

        if normalized and normalized == conv.last_incoming_normalized:
            conv.identical_incoming_streak += 1
        else:
            conv.identical_incoming_streak = 1
        conv.last_incoming_normalized = normalized
        conv.record_incoming(message, req.turn_number)

        # 1. Hostile / explicit opt-out — highest priority, always ends.
        if _contains_any(text, OPT_OUT_PATTERNS) or _contains_any(text, HOSTILE_PATTERNS):
            self.suppressions.opt_out_merchant(conv.merchant_id, time.time())
            conv.status = "ended"
            return {
                "action": "end",
                "rationale": "Merchant signaled opt-out/frustration; closing gracefully and suppressing further sends to this merchant.",
            }

        # 2. Auto-reply / canned detection.
        if _looks_canned(text) or conv.identical_incoming_streak >= 2:
            if conv.identical_incoming_streak >= 3:
                conv.status = "ended"
                return {
                    "action": "end",
                    "rationale": "Same canned message 3+ times in a row — no real engagement signal; closing.",
                }
            if conv.identical_incoming_streak == 2:
                conv.status = "waiting"
                return {
                    "action": "wait",
                    "wait_seconds": config.AUTO_REPLY_WAIT_SECONDS,
                    "rationale": "Same auto-reply repeated once already — owner likely not at the phone; backing off.",
                }
            # First time we see it: one gentle nudge, then stop wasting turns.
            conv.autoreply_nudged = True
            body_en = "Looks like an auto-reply \U0001F60A — when the owner sees this, just reply 'Yes' to continue."
            body_hi = "Yeh auto-reply lag raha hai \U0001F60A — jab owner dekhein, bas 'Yes' reply kar dein continue karne ke liye."
            body = self._dedupe(conv, [body_hi if lang == "hi_en" else body_en])
            conv.record_send(body, req.turn_number)
            return {"action": "send", "body": body, "cta": "binary_yes_no", "rationale": "Detected a likely canned auto-reply; one explicit prompt to flag it for the real owner before backing off."}

        # 3. Deferral.
        if _contains_any(text, DEFERRAL_PATTERNS) and not _contains_any(text, ACTION_INTENT_PATTERNS):
            conv.status = "waiting"
            return {
                "action": "wait",
                "wait_seconds": config.DEFAULT_WAIT_SECONDS,
                "rationale": "Merchant asked for time; backing off instead of pushing.",
            }

        # 4. Explicit action-intent — switch straight to action, never re-qualify.
        starts_with_accept = text.strip().startswith(("yes", "yeah", "sure", "ok", "okay", "yup", "y "))
        is_action_intent = _contains_any(text, ACTION_INTENT_PATTERNS) or (starts_with_accept and "?" not in text)
        is_qualifying_from_merchant = _contains_any(text, QUALIFYING_PATTERNS)
        if is_action_intent and not is_qualifying_from_merchant:
            body_en = f"Great — {label}. I'll have it ready shortly. Reply CONFIRM once you've seen it, or tell me if anything should change."
            body_hi = f"Great — {label}. Thodi der mein ready ho jaayega. Dekhne ke baad CONFIRM reply karein, ya kuch change karna ho to bataayein."
            body = self._dedupe(conv, [body_hi if lang == "hi_en" else body_en])
            conv.record_send(body, req.turn_number)
            return {
                "action": "send",
                "body": body,
                "cta": "binary_confirm_cancel",
                "rationale": "Merchant gave explicit affirmative intent; switching straight to action instead of asking another qualifying question.",
            }

        # 5. Off-topic curveball — stay polite, redirect back to the mission.
        # Deliberately conservative: only an explicit unrelated-domain marker
        # trips this, never "any question the composer doesn't recognize" —
        # a genuine follow-up question about the trigger itself should be
        # acknowledged (step 7), not deflected.
        if _contains_any(text, OFFTOPIC_PATTERNS):
            body_en = f"That's outside what I can help with directly. Coming back to where we left off — want me to go ahead with {label}?"
            body_hi = f"Yeh mere scope se bahar hai. Wapas waha aate hain jaha the — {label}? Aage badhu?"
            body = self._dedupe(conv, [body_hi if lang == "hi_en" else body_en])
            conv.record_send(body, req.turn_number)
            return {
                "action": "send",
                "body": body,
                "cta": "open_ended",
                "rationale": "Out-of-scope ask politely declined; redirected back to the original trigger without losing the thread.",
            }

        # 6. Turn cap reached without resolution.
        if req.turn_number >= config.MAX_CONVERSATION_TURNS:
            conv.status = "ended"
            return {
                "action": "end",
                "rationale": "Reached the conversation turn cap without a clear resolution; closing gracefully rather than looping.",
            }

        # 7. Default: acknowledge and advance toward the next concrete step.
        variants_en = [
            f"Got it. Want me to go ahead with {label}?",
            f"Noted — {label} is the natural next step. Shall I proceed?",
            f"Understood. I can start {label} right away — good to go?",
        ]
        variants_hi = [
            f"Samajh gayi. {label} — aage badhu?",
            f"Theek hai — {label} agla step hai. Shuru karu?",
            f"Got it. Main {label} abhi shuru kar sakti hoon — chalega?",
        ]
        variants = variants_hi if lang == "hi_en" else variants_en
        body = self._dedupe(conv, variants)
        conv.record_send(body, req.turn_number)
        return {
            "action": "send",
            "body": body,
            "cta": "binary_yes_no",
            "rationale": "Acknowledged the reply and advanced toward the next concrete step tied to the original trigger.",
        }
