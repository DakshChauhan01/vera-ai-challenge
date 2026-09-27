"""The phrasing layer.

Turns a pre-selected `Facts` bundle (app/facts.py) into WhatsApp copy. This
module NEVER looks at raw context to invent new facts — it only arranges
`facts.values` (plus a handful of always-safe structural elements: the
merchant/customer name, the category's own vocabulary list, an offer's
*title* string) into sentences. Every render function returns:

    (body: str, cta: str)

`cta` is one of: "open_ended", "binary_yes_no", "binary_confirm_cancel",
"multi_choice_slot", "none" — matching the vocabulary used throughout the
testing brief's own examples.

Anti-pattern checklist enforced structurally by every template below:
  - single CTA, always the last sentence
  - no preambles ("I hope you're doing well...")
  - no re-introduction (composer never says "Hi, I'm Vera" mid-conversation —
    that framing only belongs in the first-touch template layer, which is
    handled once per conversation by the caller, not repeated here)
  - service+price over generic discount (offer *titles* are used verbatim,
    e.g. "Dental Cleaning @ ₹299", never "X% off" unless that IS the offer)
  - no URLs, ever
"""

from __future__ import annotations

from typing import Optional

from app.facts import Facts, g
from app.language import merchant_first_name

CATEGORY_STYLE = {
    "dentists": {"unit": "patients", "verb_run": "your practice"},
    "salons": {"unit": "clients", "verb_run": "the salon"},
    "restaurants": {"unit": "covers", "verb_run": "the kitchen"},
    "gyms": {"unit": "members", "verb_run": "the floor"},
    "pharmacies": {"unit": "customers", "verb_run": "the counter"},
}


def _style(category_slug: str) -> dict:
    return CATEGORY_STYLE.get(category_slug, {"unit": "customers", "verb_run": "the business"})


def _pick(lang: str, en: str, hi: str) -> str:
    return hi if lang == "hi_en" else en


def _clean(s: Optional[str]) -> str:
    return (s or "").strip()


# --------------------------------------------------------------------------- #
# merchant-facing renderers
# --------------------------------------------------------------------------- #
def _render_research_digest(name, category, merchant, facts: Facts, lang: str):
    title = _clean(facts.get("title"))
    source = _clean(facts.get("source"))
    trial_n = facts.get("trial_n")
    segment_label = facts.get("segment_label")
    trial_clause = f"{trial_n:,}-patient trial showed " if isinstance(trial_n, (int, float)) else ""
    segment_clause = f" relevant to {segment_label}" if segment_label else ""
    source_suffix = f" — {source}" if source else ""
    body_en = (
        f"{name}, this week's digest landed. One item{segment_clause} — "
        f"{trial_clause}{title}. Worth a look (2-min read). Want me to pull it "
        f"and draft a patient-facing WhatsApp you can share?{source_suffix}"
    )
    body_hi = (
        f"{name}, is hafte ka digest aa gaya hai. Ek item{segment_clause} — "
        f"{trial_clause}{title}. 2-min ka read hai, worth a look. Kya main isse "
        f"pull karke ek WhatsApp draft bana du jo aap share kar sakein?{source_suffix}"
    )
    return _pick(lang, body_en, body_hi), "open_ended"


def _render_regulation_change(name, category, merchant, facts: Facts, lang: str):
    title = _clean(facts.get("title"))
    days = facts.get("days_until_deadline")
    actionable = _clean(facts.get("actionable"))
    deadline_clause = f" ({days} days out)" if isinstance(days, int) else ""
    action_clause = f" {actionable}." if actionable else ""
    body_en = f"{name}, compliance heads-up: {title}{deadline_clause}.{action_clause} Want me to draft a one-line SOP note for your team?"
    body_hi = f"{name}, compliance heads-up: {title}{deadline_clause}.{action_clause} Kya aapki team ke liye ek SOP note draft kar du?"
    return _pick(lang, body_en, body_hi), "open_ended"


def _render_perf(name, category, merchant, facts: Facts, lang: str, spike: bool):
    metric = _clean(facts.get("metric"))
    delta = facts.get("delta_pct_str")
    unit = _style(merchant.get("category_slug", "")).get("unit", "customers")
    is_seasonal = facts.get("is_seasonal")
    season_note = _clean(facts.get("season_note")).replace("_", " ")
    peer_ctr = facts.get("peer_ctr_str")
    merchant_ctr = facts.get("merchant_ctr_str")
    # Only attach the CTR peer-benchmark clause when the metric being
    # reported IS ctr -- category.peer_stats only carries a CTR benchmark,
    # so printing it next to a views/calls % change would compare two
    # different units (e.g. "views dipped 23% (peer median is 3.8%)" reads
    # as if 3.8% were a views benchmark, but it's a CTR level).
    peer_clause = f" (peer median CTR is {peer_ctr})" if peer_ctr and merchant_ctr and metric.strip().lower() == "ctr" else ""

    if spike:
        driver = _clean(facts.get("likely_driver"))
        driver_clause = f" — likely driven by {driver}" if driver else ""
        body_en = f"{name}, {metric} is up {delta} this week{driver_clause}. Want me to double down while it's working — a quick post or a small budget nudge?"
        body_hi = f"{name}, {metric} {delta} up hai is week{driver_clause}. Kya isi pe thoda push karein — ek post ya chhota budget nudge?"
        return _pick(lang, body_en, body_hi), "binary_yes_no"

    if is_seasonal:
        body_en = (
            f"{name}, {metric} is down {delta} this week — flagging that this matches the known "
            f"seasonal pattern{(' (' + season_note + ')') if season_note else ''}, not a real problem. "
            f"Suggest holding spend for now and focusing on retention instead. Want me to draft a retention nudge for your existing {unit}?"
        )
        body_hi = (
            f"{name}, {metric} {delta} down hai is week — lekin yeh normal seasonal pattern hai"
            f"{(' (' + season_note + ')') if season_note else ''}, koi real problem nahi. Abhi spend rokna better hai, "
            f"retention pe focus karte hain. Kya aapke existing {unit} ke liye ek retention nudge draft karu?"
        )
        return _pick(lang, body_en, body_hi), "binary_yes_no"

    body_en = f"{name}, {metric} dipped {delta} this week{peer_clause}. Want me to run a quick offer to win back the drop?"
    body_hi = f"{name}, {metric} {delta} down hai is week{peer_clause}. Kya ek quick offer chalayein isse recover karne ke liye?"
    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_milestone(name, category, merchant, facts: Facts, lang: str):
    metric = _clean(facts.get("metric"))
    value_now = facts.get("value_now")
    milestone_value = facts.get("milestone_value")
    remaining = facts.get("remaining")
    body_en = (
        f"{name}, you're at {value_now} {metric}, {remaining} away from {milestone_value}. "
        f"Want me to schedule a small post for when you cross it?"
    )
    body_hi = (
        f"{name}, aap abhi {value_now} {metric} pe hain, {milestone_value} tak sirf {remaining} baaki. "
        f"Jab cross ho jaaye tab ek post schedule kar du?"
    )
    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_review_theme(name, category, merchant, facts: Facts, lang: str):
    theme = _clean(facts.get("theme"))
    occurrences = facts.get("occurrences")
    trend = _clean(facts.get("trend"))
    quote = _clean(facts.get("quote"))
    quote_clause = f' One review said: "{quote}."' if quote else ""
    trend_clause = f", trending {trend}" if trend else ""
    body_en = (
        f"{name}, {occurrences} reviews this month mention {theme}{trend_clause}.{quote_clause} "
        f"Want me to draft a response template + a fix note for the team?"
    )
    body_hi = (
        f"{name}, is month {occurrences} reviews mein {theme} mention hua hai{trend_clause}.{quote_clause} "
        f"Kya ek response template aur team ke liye fix note draft kar du?"
    )
    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_dormant(name, category, merchant, facts: Facts, lang: str):
    days = facts.get("days_silent")
    last_topic = _clean(facts.get("last_topic"))
    topic_clause = f" — we'd left off on {last_topic}" if last_topic else ""
    body_en = f"{name}, it's been {days or 'a while'} since we last spoke{topic_clause}. Anything on your plate this week I can help move forward?"
    body_hi = f"{name}, {days or 'kaafi din'} ho gaye baat kiye{topic_clause}. Is week kuch hai jisme main help kar sakti hoon?"
    return _pick(lang, body_en, body_hi), "open_ended"


def _render_renewal_due(name, category, merchant, facts: Facts, lang: str):
    days = facts.get("days_remaining")
    plan = _clean(facts.get("plan"))
    amount = facts.get("amount")
    amount_clause = f" (₹{amount:,})" if isinstance(amount, (int, float)) else ""

    if days is not None and days <= 7:
        body_en = f"{name}, your {plan} plan renews in {days} days{amount_clause}. Want me to lock in the renewal now so nothing lapses?"
        body_hi = f"{name}, aapka {plan} plan {days} din mein renew hoga{amount_clause}. Abhi renewal lock kar du taaki kuch lapse na ho?"
    elif days is not None and days <= 30:
        body_en = f"{name}, your {plan} plan renews in {days} days{amount_clause}. Good time to lock it in early — want me to set it up now?"
        body_hi = f"{name}, aapka {plan} plan {days} din mein renew hoga{amount_clause}. Abhi lock karne ka accha time hai — set up kar du?"
    else:
        body_en = f"{name}, just a heads-up — your {plan} plan renews in {days} days{amount_clause}. Want me to set a reminder closer to the date, or lock it in early if you'd rather not think about it later?"
        body_hi = f"{name}, ek heads-up — aapka {plan} plan {days} din mein renew hoga{amount_clause}. Date ke paas reminder set karu, ya abhi hi lock kar du taaki baad mein sochna na pade?"
    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_curious_ask(name, category, merchant, facts: Facts, lang: str):
    unit = _style(merchant.get("category_slug", "")).get("unit", "customers")
    body_en = f"Quick one, {name} — what's been the most-asked-for thing from your {unit} this week? I'll turn the answer into a post plus a ready reply you can reuse. Takes 5 minutes."
    body_hi = f"Quick question, {name} — is week aapke {unit} se sabse zyada kya pucha gaya? Main isse ek post aur ek ready reply mein badal dungi. 5 minute ka kaam hai."
    return _pick(lang, body_en, body_hi), "open_ended"


def _render_active_planning(name, category, merchant, facts: Facts, lang: str):
    topic = _clean(facts.get("topic"))
    body_en = f"{name}, here's a starter draft for {topic} — want me to flesh out the full version now, or would you like to tweak the direction first?"
    body_hi = f"{name}, {topic} ke liye ek starter draft ready hai — poora version abhi banau, ya pehle direction thoda tweak karna hai?"
    return _pick(lang, body_en, body_hi), "open_ended"


def _render_winback_merchant(name, category, merchant, facts: Facts, lang: str):
    days = facts.get("days_since_expiry")
    lapsed = facts.get("lapsed_added")
    dip = facts.get("perf_dip_pct")
    lapsed_clause = f" and {lapsed} customers have gone quiet" if lapsed else ""
    body_en = (
        f"{name}, it's been {days} days since your plan lapsed — visibility is down {dip or 'noticeably'}{lapsed_clause}. "
        f"Reactivating now would put your profile back in front of them this week. Want me to send you the renewal link... "
        f"actually, want me to just flag this to your account manager to call you?"
    )
    # keep single CTA — trim the accidental double-ask above defensively
    body_en = (
        f"{name}, it's been {days} days since your plan lapsed — visibility is down {dip or 'noticeably'}{lapsed_clause}. "
        f"Reactivating now would put your profile back in front of them this week. Want me to flag this to your account manager to call you?"
    )
    body_hi = (
        f"{name}, plan lapse hue {days} din ho gaye — visibility {dip or 'kaafi'} down hai{lapsed_clause}. "
        f"Abhi reactivate karne se profile wapas unke saamne aa jaayega. Kya main aapke account manager ko call karne ke liye flag kar du?"
    )
    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_gbp_unverified(name, category, merchant, facts: Facts, lang: str):
    uplift = facts.get("uplift_str")
    path = _clean(facts.get("path")).replace("_", " ")
    uplift_clause = f" — verified profiles in your category see roughly {uplift} more calls" if uplift else ""
    body_en = f"{name}, your Google profile still isn't verified{uplift_clause}. Verification is a quick {path}. Want me to start it for you?"
    body_hi = f"{name}, aapka Google profile abhi verified nahi hai{uplift_clause}. Verification {path} se ho jaata hai, jaldi. Shuru kar du?"
    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_cde_opportunity(name, category, merchant, facts: Facts, lang: str):
    title = _clean(facts.get("title"))
    date = _clean(facts.get("date"))
    credits = facts.get("credits")
    fee = _clean(facts.get("fee")).replace("_", " ")
    credits_clause = f", {credits} CDE credits" if credits else ""
    date_clause = f" on {date.split('T')[0]}" if date else ""
    fee_clause = f" ({fee})" if fee else ""
    body_en = f"{name}, {title}{date_clause}{credits_clause}{fee_clause}. Want me to block the slot on your calendar?"
    body_hi = f"{name}, {title}{date_clause}{credits_clause}{fee_clause}. Calendar pe slot block kar du?"
    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_scheduled_recurring(name, category, merchant, facts: Facts, lang: str):
    signal = _clean(facts.get("signal")).replace("_", " ").split(":")[0]
    peer_ctr = facts.get("peer_ctr_str")
    peer_clause = f" (category median is {peer_ctr})" if peer_ctr else ""
    signal_clause = f" — still seeing {signal}{peer_clause}" if signal else ""
    body_en = f"Weekly check-in, {name}{signal_clause}. Anything you'd like me to look into this week?"
    body_hi = f"Weekly check-in, {name}{signal_clause}. Is week kuch hai jo main dekhun?"
    return _pick(lang, body_en, body_hi), "open_ended"


def _render_competitor_opened(name, category, merchant, facts: Facts, lang: str):
    competitor = _clean(facts.get("competitor_name"))
    distance = facts.get("distance_km")
    their_offer = _clean(facts.get("their_offer"))
    own_offer = _clean(facts.get("own_offer"))
    their_clause = f" running \"{their_offer}\"" if their_offer else ""
    own_clause = f" Your own \"{own_offer}\" already covers similar ground." if own_offer else ""
    body_en = f"{name}, heads-up — {competitor} opened {distance}km away{their_clause}.{own_clause} Want me to sharpen your GBP description to stand out this week?"
    body_hi = f"{name}, heads-up — {competitor} khula hai {distance}km door{their_clause}.{own_clause} Kya aapka GBP description isi week sharpen kar du?"
    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_festival(name, category, merchant, facts: Facts, lang: str):
    festival = _clean(facts.get("festival"))
    days = facts.get("days_until")
    offer = _clean(facts.get("offer_title"))
    offer_clause = f" Your \"{offer}\" is a natural fit." if offer else ""

    if days is not None and days <= 14:
        body_en = f"{name}, {festival} is just {days} days out.{offer_clause} Want me to draft a festival post now so it's ready in time?"
        body_hi = f"{name}, {festival} sirf {days} din door hai.{offer_clause} Kya abhi ek festival post draft kar du taaki time pe ready ho?"
    elif days is not None and days <= 60:
        body_en = f"{name}, {festival} is {days} days out — good time to start planning.{offer_clause} Want a draft ready early so you're not rushing later?"
        body_hi = f"{name}, {festival} {days} din door hai — planning shuru karne ka accha time hai.{offer_clause} Kya ek early draft taiyar kar du taaki baad mein rush na ho?"
    else:
        body_en = f"{name}, {festival} is {days} days out — early, but worth flagging.{offer_clause} Want me to pencil in a draft now so you're ahead of the rush when it gets closer?"
        body_hi = f"{name}, {festival} {days} din door hai — abhi jaldi hai, par flag karna zaroori tha.{offer_clause} Kya abhi ek draft pencil kar du taaki closer aane par aap aage rahe?"

    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_match_day(name, category, merchant, facts: Facts, lang: str):
    match = _clean(facts.get("match"))
    venue = _clean(facts.get("venue"))
    is_weeknight = facts.get("is_weeknight")
    note = _clean(facts.get("category_note"))
    offer = _clean(facts.get("offer_title"))
    venue_clause = f" at {venue}" if venue else ""
    if is_weeknight is False and note:
        body_en = f"Heads-up {name} — {match}{venue_clause} tonight. {note} Skip the match-night push today"
        body_en += f"; instead lean on your existing \"{offer}\" as a delivery-only special." if offer else "."
        body_en += " Want me to draft that as a quick banner?"
        body_hi = f"Heads-up {name} — {match}{venue_clause} aaj raat. {note} Aaj match-night push skip karein"
        body_hi += f"; iske bajaye \"{offer}\" ko delivery-only special ki tarah push karein." if offer else "."
        body_hi += " Ek quick banner draft kar du?"
    else:
        body_en = f"{name}, {match}{venue_clause} tonight — good night for a match-night push."
        body_en += f" Want me to promote your \"{offer}\" for tonight?" if offer else " Want me to draft a quick match-night offer?"
        body_hi = f"{name}, {match}{venue_clause} aaj raat — match-night push ke liye accha din hai."
        body_hi += f" Aaj \"{offer}\" promote kar du?" if offer else " Ek match-night offer draft kar du?"
    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_weather(name, category, merchant, facts: Facts, lang: str):
    temp = facts.get("temp_c")
    city = _clean(facts.get("city"))
    note = _clean(facts.get("category_note"))
    note_clause = f" {note}" if note else ""
    body_en = f"{name}, {temp}°C in {city} today.{note_clause} Want me to draft a heat-relevant post for this week?"
    body_hi = f"{name}, aaj {city} mein {temp}°C hai.{note_clause} Is week ke liye ek heat-relevant post draft kar du?"
    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_local_news(name, category, merchant, facts: Facts, lang: str):
    headline = _clean(facts.get("headline"))
    impact = _clean(facts.get("impact"))
    impact_clause = f" — {impact}" if impact else ""
    body_en = f"{name}, heads-up: {headline}{impact_clause}. Want me to check if it's worth adjusting anything this week?"
    body_hi = f"{name}, heads-up: {headline}{impact_clause}. Is week kuch adjust karna chahiye, check kar du?"
    return _pick(lang, body_en, body_hi), "open_ended"


def _render_category_trend(name, category, merchant, facts: Facts, lang: str):
    query = _clean(facts.get("query"))
    delta = facts.get("delta_yoy_str")
    segment = _clean(facts.get("segment_age"))
    segment_clause = f" (mostly {segment})" if segment else ""
    body_en = f"{name}, \"{query}\" searches are up {delta} YoY{segment_clause} in your category. Want me to check if your listing shows up for it?"
    body_hi = f"{name}, \"{query}\" searches {delta} YoY up hain{segment_clause} aapki category mein. Check karu ki aapki listing dikhti hai ya nahi?"
    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_category_seasonal(name, category, merchant, facts: Facts, lang: str):
    season = _clean(facts.get("season"))
    trends = facts.get("trends") or []
    trends_str = ", ".join(trends[:3])
    body_en = f"{name}, {season} shift is here — {trends_str}. Want me to suggest a shelf/menu rearrange based on this?"
    body_hi = f"{name}, {season} shift shuru ho gayi hai — {trends_str}. Kya isके hisaab se rearrange suggest karu?"
    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_generic(name, category, merchant, facts: Facts, lang: str, trigger: dict):
    kind_label = _clean(facts.get("kind_label"))
    payload_facts = facts.get("payload_facts") or []
    signal = _clean(facts.get("signal")).replace("_", " ").split(":")[0]
    fact_bits = ", ".join(f"{k}: {v}" for k, v in payload_facts) if payload_facts else signal
    if fact_bits:
        body_en = f"{name}, quick flag on {kind_label} — {fact_bits}. Want me to look into this further?"
        body_hi = f"{name}, {kind_label} pe ek quick flag — {fact_bits}. Isse aur dekhu?"
    else:
        body_en = f"{name}, a {kind_label} came up on your account. Want me to take a closer look and get back to you?"
        body_hi = f"{name}, aapke account mein ek {kind_label} aaya hai. Main isse dekh kar aapko batau?"
    return _pick(lang, body_en, body_hi), "open_ended"


# --------------------------------------------------------------------------- #
# customer-facing (send_as = merchant_on_behalf) renderers
# --------------------------------------------------------------------------- #
def _render_recall_due(cust_name, biz_name, category, merchant, facts: Facts, lang: str):
    service = _clean(facts.get("service"))
    is_generic_service = bool(facts.get("service_is_generic"))
    slots = facts.get("slots") or []
    months_str = facts.get("months_since_str")
    offer = _clean(facts.get("offer_title"))
    slots_clause = " or ".join(slots) if slots else "a time that works for you"
    offer_clause = f" {offer}." if offer else ""

    if is_generic_service:
        # No named service in the trigger payload — anchor on the
        # customer's own visit history instead of naming a service we were
        # never actually given (avoids the "your your usual visit recall is
        # due" double-possessive this would otherwise produce).
        when_en = f"It's been {months_str} since your last visit" if months_str else "It's about time for your next visit"
        when_hi = f"{months_str} ho gaye aapki last visit ko" if months_str else "Aapki agli visit ka time ho gaya hai"
        body_en = (
            f"Hi {cust_name}, {biz_name} here. {when_en} — thought I'd check in. "
            f"We have {slots_clause} available.{offer_clause} Reply with the slot that works, or tell us a better time."
        )
        body_hi = (
            f"Hi {cust_name}, {biz_name} yahan se. {when_hi} — socha check-in kar lu. "
            f"Apke liye {slots_clause} available hai.{offer_clause} Jo slot theek ho reply karein, ya koi aur time bataayein."
        )
        return _pick(lang, body_en, body_hi), ("multi_choice_slot" if len(slots) > 1 else "open_ended")

    lead_en = f"It's been {months_str} since your last visit — your" if months_str else "Your"
    lead_hi = f"{months_str} ho gaye aapki last visit ko — aapka" if months_str else "Aapka"

    body_en = (
        f"Hi {cust_name}, {biz_name} here. {lead_en} {service} recall is due. "
        f"We have {slots_clause} available.{offer_clause} Reply with the slot that works, or tell us a better time."
    )
    body_hi = (
        f"Hi {cust_name}, {biz_name} yahan se. {lead_hi} {service} recall due hai. "
        f"Apke liye {slots_clause} available hai.{offer_clause} Jo slot theek ho reply karein, ya koi aur time bataayein."
    )
    return _pick(lang, body_en, body_hi), ("multi_choice_slot" if len(slots) > 1 else "open_ended")


def _render_customer_lifecycle(cust_name, biz_name, category, merchant, facts: Facts, lang: str, hard: bool):
    days_str = facts.get("days_since_str") or "a while"
    focus = _clean(facts.get("previous_focus"))
    offer = _clean(facts.get("offer_title"))
    focus_clause = f" that fits your {focus} goal" if focus else ""
    offer_clause = f" {offer}, no pressure." if offer else " no pressure."
    weeks = days_str
    body_en = (
        f"Hi {cust_name}, {biz_name} here. It's been {weeks} — happens to everyone, no judgment. "
        f"We've got something new{focus_clause} if you'd like to pick back up.{offer_clause} Want me to hold a spot for you?"
    )
    body_hi = (
        f"Hi {cust_name}, {biz_name} yahan se. {weeks} ho gaye — aisa sabke saath hota hai, no judgment. "
        f"Kuch naya hai{focus_clause} agar aap dobara start karna chahein.{offer_clause} Ek spot hold kar du aapke liye?"
    )
    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_trial_followup(cust_name, biz_name, category, merchant, facts: Facts, lang: str):
    options = facts.get("options") or []
    if options:
        options_str = " or ".join(options)
        body_en = f"Hi {cust_name}, {biz_name} here. Hope you enjoyed the trial! Next session is open for {options_str}. Want me to book it?"
        body_hi = f"Hi {cust_name}, {biz_name} yahan se. Trial kaisa laga? Next session ke liye {options_str} available hai. Book kar du?"
    else:
        body_en = f"Hi {cust_name}, {biz_name} here. Hope you enjoyed the trial! Want me to find a slot for your next session?"
        body_hi = f"Hi {cust_name}, {biz_name} yahan se. Trial kaisa laga? Next session ke liye slot dhundh du?"
    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_chronic_refill(cust_name, biz_name, category, merchant, facts: Facts, lang: str):
    molecules = facts.get("molecules") or []
    is_generic = bool(facts.get("molecules_is_generic"))
    runs_out = _clean(facts.get("runs_out")).split("T")[0]
    offer = _clean(facts.get("offer_title"))
    delivery_saved = facts.get("delivery_saved")
    delivery_clause = " Free delivery to your saved address." if offer and "deliver" in offer.lower() else (" We have your delivery address saved." if delivery_saved else "")

    if is_generic or not molecules:
        # No molecule list available anywhere (payload or customer record) --
        # name the trigger's own reason instead of a content-free "help with
        # this?": this kind only ever fires when a chronic refill genuinely
        # is coming up for this customer, so saying that plainly is real,
        # sourced information, just not a specific one.
        body_en = f"Hi {cust_name}, {biz_name} here. Your regular refill is coming up.{delivery_clause} Reply CONFIRM to dispatch the same refill, or call us if anything's changed."
        body_hi = f"Hi {cust_name}, {biz_name} yahan se. Aapka regular refill aane wala hai.{delivery_clause} Same refill ke liye CONFIRM reply karein, ya kuch change ho to call karein."
        return _pick(lang, body_en, body_hi), "binary_confirm_cancel"

    mol_str = ", ".join(molecules[:3])
    be_verb = "is" if len(molecules[:3]) == 1 else "are"
    date_clause = f" run out around {runs_out}" if runs_out else f"{be_verb} due for a refill"
    body_en = f"Hi {cust_name}, {biz_name} here. Your {mol_str} {date_clause}.{delivery_clause} Reply CONFIRM to dispatch the same refill, or call us if anything's changed."
    body_hi = f"Hi {cust_name}, {biz_name} yahan se. Aapki {mol_str} {date_clause}.{delivery_clause} Same refill ke liye CONFIRM reply karein, ya kuch change ho to call karein."
    return _pick(lang, body_en, body_hi), "binary_confirm_cancel"


def _render_appointment_tomorrow(cust_name, biz_name, category, merchant, facts: Facts, lang: str):
    slot = _clean(facts.get("slot"))
    service = _clean(facts.get("service"))
    slot_clause = f" at {slot}" if slot else ""
    service_clause = f" for your {service}" if service else ""
    body_en = f"Hi {cust_name}, reminder from {biz_name}: you're booked{service_clause} tomorrow{slot_clause}. Reply YES to confirm or let us know if you need to reschedule."
    body_hi = f"Hi {cust_name}, {biz_name} se reminder: kal{service_clause}{slot_clause} aapki booking hai. Confirm karne ke liye YES reply karein, ya reschedule chahiye to bataayein."
    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_wedding_followup(cust_name, biz_name, category, merchant, facts: Facts, lang: str):
    days = facts.get("days_to_wedding")
    next_step = _clean(facts.get("next_step")).replace("_", " ")

    if days is not None and days <= 14:
        body_en = f"Hi {cust_name}, {biz_name} here. Only {days} days to the big day — let's lock in final touches for {next_step}. Want me to hold your slot?"
        body_hi = f"Hi {cust_name}, {biz_name} yahan se. Sirf {days} din baaki — {next_step} ke final touches lock karte hain. Aapka slot hold kar du?"
    elif days is not None and days <= 90:
        body_en = f"Hi {cust_name}, {biz_name} here. {days} days to the big day — perfect window to start {next_step}. Want me to hold your preferred slot for the first session?"
        body_hi = f"Hi {cust_name}, {biz_name} yahan se. Wedding mein {days} din baaki — {next_step} shuru karne ka sahi time hai. Pehle session ke liye aapka preferred slot hold kar du?"
    else:
        body_en = f"Hi {cust_name}, {biz_name} here. {days} days to go — early days, but wanted to flag {next_step} so you can plan ahead. Want details on timing whenever you're ready?"
        body_hi = f"Hi {cust_name}, {biz_name} yahan se. {days} din baaki hain — abhi jaldi hai, par {next_step} ke baare mein bata dena chaha taaki aap plan kar sakein. Jab ready ho tab timing details bhej du?"
    return _pick(lang, body_en, body_hi), "binary_yes_no"


def _render_generic_customer(cust_name, biz_name, category, merchant, facts: Facts, lang: str, trigger: dict):
    kind_label = _clean(facts.get("kind_label"))
    payload_facts = facts.get("payload_facts") or []
    fact_bits = ", ".join(f"{k}: {v}" for k, v in payload_facts) if payload_facts else ""
    fact_clause = f" — {fact_bits}" if fact_bits else ""
    body_en = f"Hi {cust_name}, {biz_name} here{fact_clause}. Want us to help with this?"
    body_hi = f"Hi {cust_name}, {biz_name} yahan se{fact_clause}. Isme help karein?"
    return _pick(lang, body_en, body_hi), "open_ended"


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
_MERCHANT_RENDERERS = {
    "research_digest": _render_research_digest,
    "category_research_digest_release": _render_research_digest,
    "regulation_change": _render_regulation_change,
    "perf_dip": lambda n, c, m, f, l: _render_perf(n, c, m, f, l, spike=False),
    "seasonal_perf_dip": lambda n, c, m, f, l: _render_perf(n, c, m, f, l, spike=False),
    "perf_spike": lambda n, c, m, f, l: _render_perf(n, c, m, f, l, spike=True),
    "milestone_reached": _render_milestone,
    "review_theme_emerged": _render_review_theme,
    "dormant_with_vera": _render_dormant,
    "renewal_due": _render_renewal_due,
    "curious_ask_due": _render_curious_ask,
    "active_planning_intent": _render_active_planning,
    "winback_eligible": _render_winback_merchant,
    "gbp_unverified": _render_gbp_unverified,
    "cde_opportunity": _render_cde_opportunity,
    "scheduled_recurring": _render_scheduled_recurring,
    "competitor_opened": _render_competitor_opened,
    "festival_upcoming": _render_festival,
    "ipl_match_today": _render_match_day,
    "match_day": _render_match_day,
    "weather_heatwave": _render_weather,
    "local_news_event": _render_local_news,
    "category_trend_movement": _render_category_trend,
    "category_seasonal": _render_category_seasonal,
    "supply_alert": lambda n, c, m, f, l: (
        _pick(
            l,
            f"{n}, urgent: voluntary recall on {_clean(f.get('molecule'))}"
            f"{(' batches ' + ', '.join(f.get('batches') or [])) if f.get('batches') else ''}"
            f"{(' by ' + _clean(f.get('manufacturer'))) if f.get('manufacturer') else ''} — no safety risk, but customers "
            f"should be informed for replacement."
            + (f" Up to {f.get('chronic_count')} of your chronic-Rx customers could be affected." if f.get("chronic_count") else "")
            + " Want me to draft their WhatsApp note + the replacement-pickup workflow?",
            f"{n}, urgent: {_clean(f.get('molecule'))} ke"
            f"{(' batches ' + ', '.join(f.get('batches') or [])) if f.get('batches') else ''}"
            f"{(' (' + _clean(f.get('manufacturer')) + ')') if f.get('manufacturer') else ''} par voluntary recall hai — safety risk nahi, "
            "lekin customers ko batana zaroori hai."
            + (f" Aapke {f.get('chronic_count')} chronic-Rx customers tak affected ho sakte hain." if f.get("chronic_count") else "")
            + " Unka WhatsApp note aur replacement-pickup workflow draft kar du?",
        ),
        "open_ended",
    ),
}

_CUSTOMER_RENDERERS = {
    "recall_due": _render_recall_due,
    "customer_lapsed_soft": lambda cn, bn, c, m, f, l: _render_customer_lifecycle(cn, bn, c, m, f, l, hard=False),
    "customer_lapsed_hard": lambda cn, bn, c, m, f, l: _render_customer_lifecycle(cn, bn, c, m, f, l, hard=True),
    "trial_followup": _render_trial_followup,
    "chronic_refill_due": _render_chronic_refill,
    "appointment_tomorrow": _render_appointment_tomorrow,
    "wedding_package_followup": _render_wedding_followup,
}


def compose(kind: str, category: dict, merchant: dict, trigger: dict, customer: Optional[dict], facts: Facts, lang: str) -> tuple[str, str]:
    """Returns (body, cta). Caller (decision_engine) handles send_as,
    suppression_key, rationale, and anti-hallucination validation."""
    name = merchant_first_name(merchant)
    if customer is not None:
        cust_name = g(customer, "identity", "name") or "there"
        biz_name = g(merchant, "identity", "name") or name
        fn = _CUSTOMER_RENDERERS.get(kind)
        if fn:
            return fn(cust_name, biz_name, category, merchant, facts, lang)
        return _render_generic_customer(cust_name, biz_name, category, merchant, facts, lang, trigger)

    fn = _MERCHANT_RENDERERS.get(kind)
    if fn:
        return fn(name, category, merchant, facts, lang)
    return _render_generic(name, category, merchant, facts, lang, trigger)
