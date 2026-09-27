"""Anti-hallucination validator + safe template fallback.

Per the build spec: "extract every number, date, and named entity from the
generated body and confirm each exists in the source context objects;
fall back to the raw template if not."

Implementation:
  1. Build a `source_blob` — the literal JSON serialization of every context
     object involved (category, merchant, trigger, customer). This *is* "the
     source context objects" the spec refers to.
  2. Extract every numeric token from the composed body.
  3. A token passes if it appears (digit-normalized) in `source_blob`, OR in
     the extractor's own `derived_tokens` (values the decision layer
     *computed* from sourced numbers — e.g. a ratio turned into a percentage,
     a day-count computed from two dates — which are legitimate but won't
     appear as a literal substring anywhere), OR is a small structural digit
     (1-5) used for numbering a multi-choice reply, which carries no factual
     claim on its own.
  4. Body is also rejected if it contains a URL (hard fail per the testing
     brief's examples) or a category taboo phrase (voice.vocab_taboo).
  5. On any failure, the caller substitutes `safe_fallback()` — a message
     that only ever uses the merchant's own name and the trigger's own kind/
     urgency (always literally present, so it can never fail its own check).
"""

from __future__ import annotations

import json
import re
from typing import Optional

from app.facts import Facts, g
from app.language import merchant_first_name

_NUM_RE = re.compile(r"\d[\d,]*\.?\d*")
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_STRUCTURAL_SMALL_INTS = {"1", "2", "3", "4", "5"}


def _normalize_digits(token: str) -> str:
    return token.replace(",", "").rstrip(".")


def _build_source_blob(category: dict, merchant: dict, trigger: dict, customer: Optional[dict]) -> str:
    parts = [
        json.dumps(category or {}, ensure_ascii=False),
        json.dumps(merchant or {}, ensure_ascii=False),
        json.dumps(trigger or {}, ensure_ascii=False),
        json.dumps(customer or {}, ensure_ascii=False),
    ]
    return "\n".join(parts)


def validate(
    body: str,
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict],
    facts: Facts,
) -> tuple[bool, str]:
    if not body or not body.strip():
        return False, "empty_body"

    if _URL_RE.search(body):
        return False, "url_in_body"

    if re.search(r"\bNone\b", body):
        # A literal "None" almost always means an extractor field was
        # missing and a template interpolated it anyway — a bug, not a fact.
        return False, "literal_none_leaked"

    taboos = g(category, "voice", "vocab_taboo") or []
    lowered = body.lower()
    for taboo in taboos:
        # Skip the conditional taboo entry (dataset includes a note-to-self
        # style "FDA-approved (use only when actually applicable)" — that's
        # guidance, not a literal banned phrase).
        core = taboo.split(" (")[0].strip().lower()
        if core and core in lowered:
            return False, f"taboo_phrase:{core}"

    source_blob = _normalize_digits(_build_source_blob(category, merchant, trigger, customer))
    derived_allow = {_normalize_digits(str(t)) for t in facts.derived_tokens}

    for raw_token in _NUM_RE.findall(body):
        token = _normalize_digits(raw_token)
        if not token or token in _STRUCTURAL_SMALL_INTS:
            continue
        if token in derived_allow:
            continue
        if token in source_blob:
            continue
        return False, f"unsourced_number:{raw_token}"

    return True, "ok"


def safe_fallback(category: dict, merchant: dict, trigger: dict, customer: Optional[dict], lang: str) -> tuple[str, str]:
    """Guaranteed-safe message: only ever uses the merchant's own name
    (always present in context) and the trigger's own kind — both literal,
    sourced strings, so this can never itself fail `validate()`."""
    name = merchant_first_name(merchant)
    kind_label = (trigger.get("kind") or "update").replace("_", " ")
    if customer:
        cust_name = g(customer, "identity", "name") or "there"
        biz_name = g(merchant, "identity", "name") or name
        body_en = f"Hi {cust_name}, {biz_name} here — following up on a {kind_label} update. Want us to help with this?"
        body_hi = f"Hi {cust_name}, {biz_name} yahan se — ek {kind_label} update ke baare mein. Isme help karein?"
    else:
        body_en = f"{name}, a {kind_label} update came up worth a look. Want me to walk you through it?"
        body_hi = f"{name}, ek {kind_label} update aaya hai jo dekhne layak hai. Aapko samjha du?"
    body = body_hi if lang == "hi_en" else body_en
    return body, "open_ended"
