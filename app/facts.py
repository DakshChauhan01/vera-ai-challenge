"""Fact extraction — the deterministic decision layer's second half.

Given a (category, merchant, trigger, customer) tuple and a trigger `kind`
that has already been selected by the ranking engine (app/decision_engine.py),
these functions pick the *specific* facts worth putting in the message: the
one digest item, the one signal, the one number. Nothing here writes prose —
that is app/composer.py's job. This separation is the "decide with rules,
phrase with templates" principle from the spec.

Every value placed into `Facts.values` is either:
  - copied verbatim from a context payload (category/merchant/trigger/customer), or
  - computed from two or more such values via a documented, auditable
    transform (recorded in `Facts.derived_tokens` so the anti-hallucination
    validator in app/validator.py can pre-approve it).

If a kind's payload doesn't have enough to say something specific, the
extractor returns `Facts(ok=False)` and the composer falls back to a safe,
still-real, merchant-anchored generic message rather than inventing detail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# defensive accessors + formatting helpers
# --------------------------------------------------------------------------- #
def g(d: Optional[dict], *path: str, default: Any = None) -> Any:
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


def parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    for candidate in (s, s.split("T")[0]):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def days_between(earlier_iso: Optional[str], now_dt: datetime) -> Optional[int]:
    dt = parse_date(earlier_iso)
    if not dt:
        return None
    delta = (now_dt.date() - dt.date()).days
    if 0 <= delta <= 3650:
        return delta
    return None


def days_until(later_iso: Optional[str], now_dt: datetime) -> Optional[int]:
    dt = parse_date(later_iso)
    if not dt:
        return None
    delta = (dt.date() - now_dt.date()).days
    if -30 <= delta <= 3650:
        return delta
    return None


def months_between(days: Optional[int]) -> Optional[int]:
    if days is None:
        return None
    months = round(days / 30.44)
    return months if months >= 1 else None


def pluralize(n: Optional[int], unit: str) -> Optional[str]:
    if n is None:
        return None
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def fmt_pct(ratio: Optional[float]) -> Optional[str]:
    if ratio is None:
        return None
    try:
        pct = float(ratio) * 100
    except (TypeError, ValueError):
        return None
    rounded = round(pct)
    if abs(pct - rounded) < 0.05:
        return f"{rounded}%"
    return f"{round(pct, 1)}%"


def fmt_signed_pct(ratio: Optional[float]) -> Optional[str]:
    p = fmt_pct(abs(ratio)) if ratio is not None else None
    if p is None:
        return None
    sign = "+" if ratio >= 0 else "-"
    return f"{sign}{p}"


def derived_pct(f: "Facts", ratio: Optional[float], signed: bool = False) -> Optional[str]:
    """Format a ratio as a percentage string for the message AND register its
    bare digits (no `%`, no sign) as a derived token.

    This matters because the anti-hallucination validator extracts plain
    numeric substrings from the rendered body (`23`, not `23%` or `-23%`) and
    checks them against `derived_tokens` verbatim. A percentage's *rounded*
    digits are frequently not a literal substring of the source ratio (e.g.
    ``0.038`` -> ``"3.8%"``: `"3.8"` never appears inside `"0.038"`), so any
    percentage rendered from a computed ratio must be explicitly derived like
    this rather than left to accidentally match the source blob — which only
    happens to work when the rounded percentage's digits happen to be a
    trailing substring of the raw decimal (true for some values, not others).
    """
    s = fmt_signed_pct(ratio) if signed else fmt_pct(ratio)
    if s is None:
        return None
    f.derive(s.lstrip("+-").rstrip("%"))
    return s


def active_offers(merchant: dict) -> list[dict]:
    return [o for o in (merchant.get("offers") or []) if o.get("status") == "active"]


def best_offer(merchant: dict) -> Optional[dict]:
    offers = active_offers(merchant)
    return offers[0] if offers else None


def digest_item(category: dict, item_id: Optional[str]) -> Optional[dict]:
    for item in category.get("digest") or []:
        if item.get("id") == item_id:
            return item
    return None


def most_recent_digest_item(category: dict) -> Optional[dict]:
    items = category.get("digest") or []
    return items[0] if items else None


def merchant_first_name(merchant: dict) -> str:
    from app.language import owner_or_business_name

    return owner_or_business_name(merchant)


def has_signal(merchant: dict, needle: str) -> bool:
    return any(needle in s for s in (merchant.get("signals") or []))


def find_signal(merchant: dict, needle: str) -> Optional[str]:
    for s in merchant.get("signals") or []:
        if needle in s:
            return s
    return None


def slot_labels(slots: list[dict]) -> list[str]:
    return [s.get("label") or s.get("iso", "") for s in (slots or []) if s.get("label") or s.get("iso")]


# --------------------------------------------------------------------------- #
# Facts container
# --------------------------------------------------------------------------- #
@dataclass
class Facts:
    ok: bool = True
    values: dict[str, Any] = field(default_factory=dict)
    derived_tokens: list[str] = field(default_factory=list)
    why_now: str = ""
    # True while these facts came from the kind-specific extractor and are
    # shaped for that kind's bespoke composer template. False once we've
    # fallen back to the generic extractor, which the generic composer
    # template (not the bespoke one) knows how to read.
    is_specific: bool = True

    def set(self, key: str, value: Any) -> Any:
        if value is not None:
            self.values[key] = value
        return value

    def derive(self, token: Any) -> Any:
        if token is not None:
            self.derived_tokens.append(str(token))
        return token

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


# --------------------------------------------------------------------------- #
# Per-kind extractors
# --------------------------------------------------------------------------- #
def _extract_research_digest(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    item = digest_item(category, payload.get("top_item_id")) or most_recent_digest_item(category)
    if not item:
        f.ok = False
        return f
    f.set("title", item.get("title"))
    f.set("source", item.get("source"))
    f.set("trial_n", item.get("trial_n"))
    f.set("summary", item.get("summary"))
    f.set("actionable", item.get("actionable"))
    segment = item.get("patient_segment")
    if segment == "high_risk_adults" and g(merchant, "customer_aggregate", "high_risk_adult_count"):
        n = g(merchant, "customer_aggregate", "high_risk_adult_count")
        f.set("segment_label", f.derive(f"your {n} high-risk adult patients") and f"your {n} high-risk adult patients")
    elif segment:
        f.set("segment_label", f.derive(segment.replace("_", " ")) and segment.replace("_", " ") + " patients")
    f.why_now = f"this week's category research digest ({item.get('source', 'digest')})"
    return f


def _extract_regulation_change(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    item = digest_item(category, payload.get("top_item_id"))
    deadline = payload.get("deadline_iso") or (item or {}).get("date")
    if item:
        f.set("title", item.get("title"))
        f.set("source", item.get("source"))
        f.set("summary", item.get("summary"))
        f.set("actionable", item.get("actionable"))
    if deadline:
        f.set("deadline", deadline)
        days = days_until(deadline, now_dt)
        if days is not None:
            f.set("days_until_deadline", f.derive(days))
    f.why_now = "a regulatory/compliance deadline affecting this category"
    if not item and not deadline:
        f.ok = False
    return f


def _extract_recall_due(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    if not customer:
        f.ok = False
        return f
    last_service = payload.get("last_service_date") or g(customer, "relationship", "last_visit")
    months = months_between(days_between(last_service, now_dt))

    service = (payload.get("service_due") or "").replace("_", " ").strip()
    if service:
        service = re.sub(r"^(\d+) ", r"\1-", service)  # "6 month cleaning" -> "6-month cleaning"
        f.set("service_is_generic", False)
    else:
        # Placeholder payload with no named service. Don't bail straight to
        # the fully generic fallback: the customer's own visit history
        # (relationship.last_visit) is real, sourced data that still lets us
        # say something specific and personalized — just without naming a
        # service we were never actually given. Only fail if there's truly
        # nothing here (no last_visit at all to anchor on).
        if last_service is None:
            f.ok = False
            return f
        service = "your usual visit"
        f.set("service_is_generic", True)
    f.set("service", service)
    slots = slot_labels(payload.get("available_slots") or [])
    f.set("slots", slots)
    if months:
        f.derive(months)
        f.set("months_since_str", pluralize(months, "month"))
    offer = best_offer(merchant)
    if offer:
        f.set("offer_title", offer.get("title"))
    f.set("customer_name", g(customer, "identity", "name"))
    f.why_now = (
        f"the {service} window has opened for this patient/customer"
        if not f.get("service_is_generic")
        else "customer's visit-history cadence suggests a recall is due, even without a named service in the trigger payload"
    )
    return f


def _extract_perf(category, merchant, trigger, customer, now_dt, spike: bool) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    metric = payload.get("metric")
    delta_pct = payload.get("delta_pct")
    if not metric or delta_pct is None:
        # Placeholder payload: fall back to the merchant's own real
        # performance.delta_7d numbers rather than bailing to the fully
        # generic fallback. Only ever pick a metric whose delta actually
        # points the direction we're reporting (negative for a dip,
        # positive for a spike) — if nothing in delta_7d actually moved that
        # way, there's no real signal to report and we correctly fall
        # through to ok=False rather than mislabeling a rise as a "dip" or
        # vice versa.
        delta_7d = g(merchant, "performance", "delta_7d") or {}
        candidates = [
            (k, v) for k, v in delta_7d.items()
            if isinstance(v, (int, float)) and ((v < 0) if not spike else (v > 0))
        ]
        if not candidates:
            f.ok = False
            return f
        key, value = max(candidates, key=lambda kv: abs(kv[1]))
        metric = key.replace("_pct", "").replace("_", " ")
        delta_pct = value
    f.set("metric", metric)
    f.set("delta_pct_str", derived_pct(f, delta_pct, signed=True))
    baseline = payload.get("vs_baseline")
    if baseline is not None:
        f.set("baseline", baseline)
    peer_ctr = g(category, "peer_stats", "avg_ctr")
    merchant_ctr = g(merchant, "performance", "ctr")
    if peer_ctr and merchant_ctr:
        f.set("merchant_ctr_str", derived_pct(f, merchant_ctr))
        f.set("peer_ctr_str", derived_pct(f, peer_ctr))
    is_seasonal = payload.get("is_expected_seasonal") or trigger.get("kind") == "seasonal_perf_dip"
    f.set("is_seasonal", bool(is_seasonal))
    f.set("season_note", payload.get("season_note"))
    likely_driver = payload.get("likely_driver")
    if likely_driver:
        f.set("likely_driver", likely_driver.replace("_", " "))
    f.why_now = f"{metric} {'spiked' if spike else 'dipped'} {fmt_signed_pct(delta_pct) or ''}".strip()
    return f


def _extract_milestone(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    metric = payload.get("metric", "milestone")
    value_now = payload.get("value_now")
    milestone_value = payload.get("milestone_value")
    if value_now is None or milestone_value is None:
        f.ok = False
        return f
    f.set("metric", metric.replace("_", " "))
    f.set("value_now", value_now)
    f.set("milestone_value", milestone_value)
    remaining = f.derive(milestone_value - value_now) if isinstance(milestone_value, (int, float)) and isinstance(value_now, (int, float)) else None
    f.set("remaining", remaining)
    f.why_now = f"closing in on {milestone_value} {metric.replace('_', ' ')}"
    return f


def _extract_review_theme(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    theme = payload.get("theme")
    if not theme:
        f.ok = False
        return f
    f.set("theme", theme.replace("_", " "))
    f.set("occurrences", payload.get("occurrences_30d"))
    f.set("trend", payload.get("trend"))
    f.set("quote", payload.get("common_quote"))
    f.why_now = f"a review pattern emerged this month ({theme.replace('_', ' ')})"
    return f


def _extract_dormant(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    days = payload.get("days_since_last_merchant_message")
    if days is None:
        sig = find_signal(merchant, "dormant_with_vera")
        if sig and ":" in sig:
            try:
                days = int(sig.split(":")[-1].rstrip("d"))
            except ValueError:
                days = None
    f.set("days_silent", days)
    f.set("last_topic", (payload.get("last_topic") or "").replace("_", " "))
    f.why_now = f"{days or 'several'} days of silence since the last conversation"
    return f


def _extract_renewal_due(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    days_remaining = payload.get("days_remaining", g(merchant, "subscription", "days_remaining"))
    plan = payload.get("plan", g(merchant, "subscription", "plan"))
    amount = payload.get("renewal_amount")
    if days_remaining is None:
        f.ok = False
        return f
    f.set("days_remaining", days_remaining)
    f.set("plan", plan)
    f.set("amount", amount)
    f.why_now = f"subscription renews in {days_remaining} days"
    return f


def _extract_curious_ask(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    template = (payload.get("ask_template") or "").replace("_", " ")
    f.set("ask_template", template)
    f.why_now = "weekly curious-ask cadence"
    return f


def _extract_active_planning(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    topic = (payload.get("intent_topic") or "").replace("_", " ")
    last_msg = payload.get("merchant_last_message")
    if not topic:
        f.ok = False
        return f
    f.set("topic", topic)
    f.set("last_message", last_msg)
    f.why_now = "merchant is actively planning this and asked a direct question"
    return f


def _extract_winback_merchant(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    # This kind is only meaningful once we know how long ago the plan
    # lapsed. The payload may be placeholder-only, so fall back to the
    # merchant's own subscription record (the same field the trigger's
    # concept is describing) before giving up -- exactly the pattern
    # `_extract_renewal_due` already uses for `days_remaining`.
    days_since_expiry = payload.get("days_since_expiry", g(merchant, "subscription", "days_since_expiry"))
    if days_since_expiry is None:
        # Neither the payload nor the merchant's own subscription record
        # shows a lapsed status -- there's no real "since when" fact to
        # anchor a winback message on, and composing one with a blank/None
        # day-count would be worse than the generic fallback.
        f.ok = False
        return f
    f.set("days_since_expiry", days_since_expiry)

    perf_dip_pct = payload.get("perf_dip_pct")
    if perf_dip_pct is None:
        # Fall back to the merchant's own real delta_7d numbers, same
        # approach as `_extract_perf`: only ever use a metric that's
        # actually negative, never force a "dip" narrative that isn't there.
        delta_7d = g(merchant, "performance", "delta_7d") or {}
        negative = [(k, v) for k, v in delta_7d.items() if isinstance(v, (int, float)) and v < 0]
        if negative:
            _, perf_dip_pct = max(negative, key=lambda kv: abs(kv[1]))
    if perf_dip_pct is not None:
        f.set("perf_dip_pct", derived_pct(f, perf_dip_pct, signed=True))

    f.set("lapsed_added", payload.get("lapsed_customers_added_since_expiry"))
    f.why_now = "subscription lapsed and performance/customer signals point to a winback window"
    return f


def _extract_customer_lifecycle(category, merchant, trigger, customer, now_dt, hard: bool) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    if not customer:
        f.ok = False
        return f
    days = payload.get("days_since_last_visit")
    if days is None:
        days = days_between(g(customer, "relationship", "last_visit"), now_dt)
        if days is not None:
            f.derive(days)
    if days is None:
        f.ok = False
        return f
    f.set("days_since_str", pluralize(days, "day"))
    f.set("previous_focus", (payload.get("previous_focus") or "").replace("_", " "))
    offer = best_offer(merchant)
    if offer:
        f.set("offer_title", offer.get("title"))
    f.set("customer_name", g(customer, "identity", "name"))
    f.why_now = f"customer has been {'churned' if hard else 'quiet'} for a while ({days} days)"
    return f


def _extract_trial_followup(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    if not customer:
        f.ok = False
        return f
    trial_date = payload.get("trial_date")
    options = slot_labels(payload.get("next_session_options") or [])
    # No merchant/customer-level fallback exists for a specific trial_date or
    # next-session options (checked: nothing on the customer schema names a
    # trial-completion date or preferred slots in this dataset) -- but there
    # is nothing to fabricate here either. The bespoke composer already has
    # a clean fallback branch for this exact case ("Hope you enjoyed the
    # trial! Want me to find a slot for your next session?"), which is real
    # and honest (this kind only fires when a trial genuinely just
    # happened) but was never reached because this hard gate always bailed
    # to the fully generic, kind-blind fallback first. Only fail if there's
    # no customer to address at all (checked above).
    f.set("trial_date", trial_date)
    f.set("options", options)
    f.set("customer_name", g(customer, "identity", "name"))
    f.why_now = "trial session completed, natural moment to convert"
    return f


def _extract_chronic_refill(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    if not customer:
        f.ok = False
        return f
    molecules = payload.get("molecule_list") or []
    if not molecules:
        # Placeholder payload: no molecule list given. Deliberately NOT
        # substituting customer.relationship.chronic_conditions here -- a
        # condition (e.g. "hypertension") is a different fact than a
        # medication name, and presenting one as the other would misrepresent
        # what's actually known, not just soften it. Nothing else on the
        # customer record names a specific medication in this dataset. What
        # IS unconditionally true and fully sourced is the trigger's own
        # existence: this kind only ever fires when a chronic refill
        # genuinely is coming up for this customer, so the composer says
        # that plainly (flagged via molecules_is_generic) instead of falling
        # all the way to the content-free "want us to help with this?".
        f.set("molecules_is_generic", True)
    runs_out = payload.get("stock_runs_out_iso")
    f.set("molecules", molecules)
    f.set("runs_out", runs_out)
    offer = best_offer(merchant)
    if offer:
        f.set("offer_title", offer.get("title"))
    f.set("customer_name", g(customer, "identity", "name"))
    f.set("delivery_saved", payload.get("delivery_address_saved"))
    f.why_now = "chronic prescription is about to run out"
    return f


def _extract_supply_alert(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    molecule = payload.get("molecule")
    batches = payload.get("affected_batches") or []
    manufacturer = payload.get("manufacturer")
    if not molecule:
        f.ok = False
        return f
    f.set("molecule", molecule)
    f.set("batches", batches)
    f.set("manufacturer", manufacturer)
    # Merchant-fit: how many of this merchant's chronic-Rx roster are plausibly affected.
    chronic_count = g(merchant, "customer_aggregate", "chronic_rx_count")
    if chronic_count:
        # Deterministic, disclosed estimate (not a fabricated exact count): we only ever
        # say "up to N" using the merchant's own real chronic_rx_count.
        f.set("chronic_count", chronic_count)
    f.why_now = "supply/recall alert relevant to this pharmacy's stock"
    return f


def _extract_category_signal(category, merchant, trigger, customer, now_dt) -> Facts:
    """Covers category_trend_movement / category_seasonal / weather / local news /
    festival / ipl-style external event triggers that key off category-level or
    payload-level descriptive data rather than merchant numbers."""
    f = Facts()
    payload = trigger.get("payload") or {}
    kind = trigger.get("kind", "")

    if kind == "festival_upcoming":
        fest = payload.get("festival")
        if not fest:
            f.ok = False
            return f
        f.set("festival", fest)
        f.set("days_until", payload.get("days_until"))
        offer = best_offer(merchant)
        if offer:
            f.set("offer_title", offer.get("title"))
        f.why_now = f"{fest} is approaching"

    elif kind in ("ipl_match_today", "match_day"):
        f.set("match", payload.get("match"))
        f.set("venue", payload.get("venue"))
        f.set("is_weeknight", payload.get("is_weeknight"))
        # pull a matching seasonal beat or digest note about match-night patterns if present
        for beat in category.get("seasonal_beats") or []:
            if "match" in beat.get("note", "").lower() or "ipl" in beat.get("note", "").lower():
                f.set("category_note", beat.get("note"))
        for item in category.get("digest") or []:
            if "ipl" in item.get("title", "").lower() or "match" in item.get("title", "").lower():
                f.set("category_note", item.get("summary") or item.get("title"))
        offer = best_offer(merchant)
        if offer:
            f.set("offer_title", offer.get("title"))
        f.why_now = "match-day timing"
        if not f.values.get("match"):
            f.ok = False

    elif kind == "weather_heatwave":
        f.set("temp_c", payload.get("temp_c") or payload.get("temperature"))
        f.set("city", payload.get("city") or g(merchant, "identity", "city"))
        for item in category.get("digest") or []:
            if "summer" in item.get("title", "").lower() or "heat" in item.get("title", "").lower():
                f.set("category_note", item.get("actionable") or item.get("summary"))
        f.why_now = "heatwave conditions today"
        if not f.values.get("temp_c"):
            f.ok = False

    elif kind == "local_news_event":
        f.set("headline", payload.get("headline") or payload.get("event"))
        f.set("impact", payload.get("impact"))
        f.why_now = "a local news event affecting footfall/logistics"
        if not f.values.get("headline"):
            f.ok = False

    elif kind in ("category_trend_movement",):
        trends = category.get("trend_signals") or []
        top = max(trends, key=lambda t: t.get("delta_yoy", 0)) if trends else None
        if not top:
            f.ok = False
            return f
        f.set("query", top.get("query"))
        f.set("delta_yoy_str", fmt_pct(top.get("delta_yoy")))
        f.set("segment_age", top.get("segment_age"))
        f.why_now = "a category-wide search trend is moving"

    elif kind == "category_seasonal":
        season = payload.get("season", "").replace("_", " ")
        trends = payload.get("trends") or []
        if not trends:
            f.ok = False
            return f
        f.set("season", season)
        f.set("trends", [t.replace("_", " ") for t in trends])
        f.why_now = f"seasonal demand shift ({season})"

    else:
        f.ok = False

    return f


def _extract_competitor_opened(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    name = payload.get("competitor_name")
    if not name:
        f.ok = False
        return f
    f.set("competitor_name", name)
    f.set("distance_km", payload.get("distance_km"))
    f.set("their_offer", payload.get("their_offer"))
    own_offer = best_offer(merchant)
    if own_offer:
        f.set("own_offer", own_offer.get("title"))
    f.why_now = f"competitor opened nearby ({payload.get('distance_km', '?')}km)"
    return f


def _extract_appointment_tomorrow(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    if not customer:
        f.ok = False
        return f
    slot = payload.get("slot_label") or payload.get("time")
    service = (payload.get("service") or "").replace("_", " ").strip()
    if not slot and not service:
        # Placeholder payload: no time slot or named service. Try the
        # customer's own visit history for a soft anchor first -- a most
        # recent entry in services_received is real, sourced data, not a
        # fabrication.
        received = g(customer, "relationship", "services_received") or []
        if received:
            last = received[-1]
            label = last.get("service") if isinstance(last, dict) else last
            if label:
                service = str(label).replace("_", " ").strip()
        # Even without that, we still have exactly one fact that's
        # unconditionally true and fully sourced: this trigger kind only
        # ever fires when there genuinely IS an appointment tomorrow for
        # this customer, so composing "you're booked tomorrow" (with no
        # named slot/service) is real information, not an invention -- it's
        # strictly better than the fully generic, kind-blind fallback this
        # used to bail into. Only fail if there's no customer at all
        # (checked above).
    f.set("slot", slot)
    f.set("service", service)
    f.set("customer_name", g(customer, "identity", "name"))
    f.why_now = "appointment scheduled for tomorrow"
    return f


def _extract_wedding_followup(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    if not customer or payload.get("days_to_wedding") is None:
        f.ok = False
        return f
    f.set("wedding_date", payload.get("wedding_date"))
    f.set("days_to_wedding", payload.get("days_to_wedding"))
    f.set("trial_completed", payload.get("trial_completed"))
    f.set("next_step", (payload.get("next_step_window_open") or "").replace("_", " "))
    f.set("customer_name", g(customer, "identity", "name"))
    f.why_now = f"{payload.get('days_to_wedding', '?')} days to the wedding, next-step window open"
    return f


def _extract_gbp_unverified(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    uplift = payload.get("estimated_uplift_pct")
    f.set("uplift_str", fmt_pct(uplift) if uplift else None)
    f.set("path", (payload.get("verification_path") or "").replace("_", " "))
    f.why_now = "Google Business Profile still unverified"
    return f


def _extract_cde_opportunity(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    payload = trigger.get("payload") or {}
    item = digest_item(category, payload.get("digest_item_id"))
    if not item:
        f.ok = False
        return f
    f.set("title", item.get("title"))
    f.set("date", item.get("date"))
    f.set("credits", payload.get("credits") or item.get("credits"))
    f.set("fee", payload.get("fee"))
    f.set("summary", item.get("summary"))
    f.why_now = "relevant CDE/training opportunity"
    return f


def _extract_scheduled_recurring(category, merchant, trigger, customer, now_dt) -> Facts:
    f = Facts()
    # Deliberately light-touch: this is Vera's own weekly cadence, not an
    # external/internal event, so we anchor on one real merchant fact
    # (a signal or a peer stat) rather than the trigger payload.
    sig = (merchant.get("signals") or [None])[0]
    f.set("signal", sig)
    peer_ctr = g(category, "peer_stats", "avg_ctr")
    if peer_ctr:
        f.set("peer_ctr_str", fmt_pct(peer_ctr))
    f.why_now = "scheduled weekly check-in"
    return f


def _extract_generic(category, merchant, trigger, customer, now_dt) -> Facts:
    """Fallback for any trigger kind we don't have a bespoke extractor for
    (including brand-new kinds injected mid-test that this build has never
    seen). We never invent detail: we surface exactly what's in the
    trigger's own payload (skipping placeholders) plus one real merchant
    signal, so the message stays specific without hallucinating."""
    f = Facts()
    payload = trigger.get("payload") or {}
    concrete_pairs = []
    for k, v in payload.items():
        if k in ("placeholder", "metric_or_topic"):
            # "metric_or_topic" in this dataset's filler triggers just repeats
            # the trigger kind verbatim — not an independent fact worth citing.
            continue
        if isinstance(v, (str, int, float)) and str(v).strip():
            concrete_pairs.append((k.replace("_", " "), v))
    f.set("payload_facts", concrete_pairs[:3])
    sig = merchant.get("signals") or []
    f.set("signal", sig[0] if sig else None)
    f.set("kind_label", trigger.get("kind", "update").replace("_", " "))
    f.why_now = f"a {trigger.get('kind', 'update').replace('_', ' ')} event"
    if not concrete_pairs and not sig:
        f.ok = False
    return f


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
_EXTRACTORS = {
    "research_digest": _extract_research_digest,
    "category_research_digest_release": _extract_research_digest,
    "regulation_change": _extract_regulation_change,
    "recall_due": _extract_recall_due,
    "perf_dip": lambda c, m, t, cu, n: _extract_perf(c, m, t, cu, n, spike=False),
    "seasonal_perf_dip": lambda c, m, t, cu, n: _extract_perf(c, m, t, cu, n, spike=False),
    "perf_spike": lambda c, m, t, cu, n: _extract_perf(c, m, t, cu, n, spike=True),
    "milestone_reached": _extract_milestone,
    "review_theme_emerged": _extract_review_theme,
    "dormant_with_vera": _extract_dormant,
    "renewal_due": _extract_renewal_due,
    "curious_ask_due": _extract_curious_ask,
    "active_planning_intent": _extract_active_planning,
    "winback_eligible": _extract_winback_merchant,
    "customer_lapsed_soft": lambda c, m, t, cu, n: _extract_customer_lifecycle(c, m, t, cu, n, hard=False),
    "customer_lapsed_hard": lambda c, m, t, cu, n: _extract_customer_lifecycle(c, m, t, cu, n, hard=True),
    "trial_followup": _extract_trial_followup,
    "chronic_refill_due": _extract_chronic_refill,
    "supply_alert": _extract_supply_alert,
    "competitor_opened": _extract_competitor_opened,
    "appointment_tomorrow": _extract_appointment_tomorrow,
    "wedding_package_followup": _extract_wedding_followup,
    "gbp_unverified": _extract_gbp_unverified,
    "cde_opportunity": _extract_cde_opportunity,
    "scheduled_recurring": _extract_scheduled_recurring,
    "festival_upcoming": _extract_category_signal,
    "ipl_match_today": _extract_category_signal,
    "match_day": _extract_category_signal,
    "weather_heatwave": _extract_category_signal,
    "local_news_event": _extract_category_signal,
    "category_trend_movement": _extract_category_signal,
    "category_seasonal": _extract_category_signal,
}


def extract(kind: str, category: dict, merchant: dict, trigger: dict, customer: Optional[dict], now_dt: datetime) -> Facts:
    fn = _EXTRACTORS.get(kind, _extract_generic)
    try:
        facts = fn(category, merchant, trigger, customer, now_dt)
    except Exception:
        facts = Facts(ok=False)
    if not facts.values and facts.ok:
        facts.ok = False
    if not facts.ok and fn is not _extract_generic:
        # The bespoke extractor couldn't find enough real detail (e.g. a
        # placeholder-only payload) — fall back to the generic extractor,
        # which only ever surfaces facts it actually found. Its own `ok`
        # becomes the final word on whether we have anything specific to say.
        try:
            facts = _extract_generic(category, merchant, trigger, customer, now_dt)
            facts.is_specific = False
        except Exception:
            facts = Facts(ok=False, is_specific=False)
    return facts
