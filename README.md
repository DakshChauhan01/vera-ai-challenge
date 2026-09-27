# Vera 2.0 — Deterministic Merchant Message Engine

A from-scratch rebuild of Vera's message-composing engine for the magicpin AI Challenge, exposed as the 5-endpoint HTTP service the judge harness expects (`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`, plus optional `/v1/teardown`).

**No LLM is called anywhere in the composition path.** Every message is produced by a deterministic decision layer + a template-based phrasing layer + an anti-hallucination validator, all pure Python, all sub-millisecond. This was a deliberate choice — see [Model / approach choice](#model--approach-choice) below.

---

## Quickstart

```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Sanity-check it against the full expanded dataset (no LLM key needed — this is a structural/quality smoke test, not the scored judge run):

```bash
python scripts/smoke_test.py
```

Run the unit test suite:

```bash
pip install pytest httpx
pytest tests/ -v
```

Run the official (LLM-scored) local judge simulator once you've set an API key in its config block:

```bash
export BOT_URL=http://localhost:8080
python judge_simulator.py
```

`dataset/` ships both the original seed files (as given in the challenge zip) and a pre-generated `dataset/expanded/` (50 merchants / 200 customers / 100 triggers / 30 test pairs), produced deterministically by `dataset/generate_dataset.py --seed-dir . --out ./expanded`.

---

## Architecture

```
app/
├── main.py             FastAPI wiring for the 5 endpoints — thin, no business logic
├── store.py             In-memory ContextStore (versioned, idempotent) + SuppressionStore + ConversationStore
├── models.py             Pydantic request models (context payloads stay plain dict — see note below)
├── facts.py              DECISION layer, part 2: per-trigger-kind fact extraction (~30 kinds + generic fallback)
├── decision_engine.py     DECISION layer, part 1: scores & ranks triggers for /v1/tick, orchestrates the pipeline
├── composer.py            PHRASING layer: template rendering per (category, trigger-kind), English + Hinglish
├── validator.py           Anti-hallucination validator + guaranteed-safe fallback template
├── conversation.py        /v1/reply state machine: auto-reply detection, intent transitions, hostility, off-topic
└── language.py            Language-preference resolution (hi_en vs en) + salutation logic
```

### Decide with rules, phrase with templates

This is the non-negotiable design principle from the spec, and the module boundaries above exist specifically to enforce it:

1. **`decision_engine.py`** looks at every trigger the judge says is "available right now", resolves its merchant + category (+ customer), and **scores** it: base score from `urgency`, a bonus if the trigger's `kind` lines up with one of the merchant's own `signals` (confirms the trigger isn't noise), a bonus if it's about to expire, a penalty if this merchant was just messaged very recently and hasn't replied. It sorts, deduplicates to one action per distinct engagement target (`merchant_id`, or `merchant_id::customer_id` for customer-scope triggers) per tick, and caps at 20.
2. **`facts.py`** then extracts, for the *one* trigger selected for each target, the specific facts worth saying — the one digest item, the one number, the one signal — never inventing anything not present in a context payload. Every derived value (a ratio turned into a percentage, a day-count computed from two dates) is explicitly recorded so the validator can pre-approve it.
3. **`composer.py`** takes those facts and arranges them into a sentence, dispatched by `(customer present?, trigger.kind)`. It never touches raw context — only `facts.values`.
4. **`validator.py`** re-extracts every number from the finished body and confirms each one traces back to the source context objects (or an explicitly-derived value); rejects any URL, any category `voice.vocab_taboo` phrase, and any literal `"None"` leak (a robust tripwire for "an extractor field was missing and got interpolated anyway" bugs). On failure, it substitutes a guaranteed-safe fallback message built only from the merchant's own name and the trigger's own `kind` — both always-sourced, so the fallback can never itself fail validation.

If a trigger's payload doesn't have enough real detail for its kind's bespoke template (this happens for filler/placeholder-payload triggers, and will happen for **any trigger kind this build has never seen before** — a real risk during Phase 3's adaptive injection), `facts.py` transparently falls back to a generic extractor that only ever surfaces whatever concrete fields *are* present in the payload plus one real merchant signal. The bot never says something generic-and-empty; it either says something specific or says less.

#### Extractor fallback audit (which bespoke extractors fall back to merchant/customer context, and which correctly don't)

75 of the 100 generated triggers in `dataset/expanded/triggers/` are placeholder-only (`{"placeholder": true, "metric_or_topic": "..."}`), which surfaced a real gap: two bespoke extractors hard-gated on `trigger.payload` fields with no fallback, even when the *merchant or customer* context they were pushed alongside had the exact fact needed — silently downgrading a specific message to the generic one instead of using data that was sitting right there.

- **`_extract_perf`** (`perf_dip` / `perf_spike`) now falls back to `merchant.performance.delta_7d` when the payload has no `metric`/`delta_pct`, picking whichever of `views_pct`/`calls_pct` actually moves in the reported direction (most negative for a dip, most positive for a spike). If nothing in `delta_7d` moves that way, it still correctly returns `ok=False` — reporting a "spike" off a merchant whose numbers are all declining would be worse than saying less, so that case is left to the generic fallback rather than force-fit.
- **`_extract_recall_due`** now falls back to `customer.relationship.last_visit` to compute months-since-last-visit even when the payload has no `service_due`, using the honest phrase "your usual visit" rather than naming a specific service we were never given. It still returns `ok=False` if there's no `last_visit` at all to anchor on.
- Fixing this also surfaced a related latent bug in the anti-hallucination validator interaction: percentages formatted from a ratio (e.g. `0.038` → `"3.8%"`) aren't always a literal substring of the source ratio's own decimal text, so they were sometimes rejected as "unsourced" even though they're a straightforward, honestly-computed value. `facts.py`'s `derived_pct()` helper now explicitly registers the bare digits as a derived token in every case, closing that gap without loosening what the validator accepts.
- **Checked for the same pattern and found no data to fall back to** (would require fabricating a fact, not just relaxing a gate) in `competitor_opened` and `milestone_reached` — neither the merchant nor customer schema anywhere in this dataset carries a competitor name or a milestone target value. Inventing either to force a specific-looking message would violate the "never fabricate data" hard constraint, so these correctly stay on the generic fallback (which, being merchant-scoped, still names the trigger's own kind — see below) until a real payload supplies the missing fact.
- **A second round found a related but distinct bug**: `_extract_appointment_tomorrow` and `_extract_chronic_refill` (both customer-scoped) also hard-gated on payload fields with no fallback, but the customer-facing generic composer (`_render_generic_customer`) — unlike its merchant-facing counterpart — never mentions the trigger's own kind at all, so a placeholder payload used to silently produce a message that didn't even say *why* Vera was messaging (e.g. `"Hi Aditya, Karim's Salon here. Want us to help with this?"` for an appointment reminder). Both are now fixed the same way as `_extract_recall_due`: `_extract_appointment_tomorrow` tries `customer.relationship.services_received`'s most recent entry as a soft anchor first, and either way stops hard-gating — the bespoke `_render_appointment_tomorrow` template already degrades gracefully to `"you're booked tomorrow"` with no slot/service, which is real (this kind only fires when there genuinely is an appointment tomorrow), just not detailed. `_extract_chronic_refill` deliberately does *not* substitute `customer.relationship.chronic_conditions` for a molecule list (a condition like "hypertension" is a different fact than a medication name, and presenting one as the other would misrepresent what's known, not just soften it); with nothing usable found, it flags `molecules_is_generic` and the composer now says `"Your regular refill is coming up"` instead of the kind-blind generic. `_extract_trial_followup` had the same hard gate guarding a composer branch that was already perfectly fine empty-handed (`"Hope you enjoyed the trial! Want me to find a slot for your next session?"`) — the gate was simply removed.
- **`_extract_winback_merchant`** (`winback_eligible`) had no explicit gate at all and could have baked a literal `None` into `"it's been None days since your plan lapsed"` if the payload was placeholder-only with no merchant fallback in play; no trigger in this dataset currently exercises that path, but it's a real risk for Phase 3 adaptive injection. Fixed proactively with the same pattern as `renewal_due`: falls back to `merchant.subscription.days_since_expiry` for the day-count (returning `ok=False` if that's also absent — no fact to anchor on at all) and to `merchant.performance.delta_7d`'s most-negative metric for `perf_dip_pct` (same honest-direction check as `_extract_perf`), before giving up.
- **`_extract_active_planning`** (`active_planning_intent`) was checked too: the merchant `signals` list only ever carries a bare `"active_planning"` flag with no embedded topic (unlike, say, `"dormant_with_vera:14d"`), so there's no real topic to fall back to, and no placeholder-payload trigger of this kind exists in the dataset regardless. Left as-is — the merchant-facing generic fallback it lands on already names the kind, so this isn't the "silently blank" failure mode above. `renewal_due` and `curious_ask_due` were both re-checked and needed no change: `renewal_due` already read `merchant.subscription.days_remaining`/`.plan` as a fallback default from day one, and `curious_ask_due`'s composer never depended on payload content in the first place (it's Vera's own fixed weekly-cadence question, deliberately payload-agnostic, same as `scheduled_recurring`).
- **Composer bug, found while re-reviewing `_render_perf`**: the peer-comparison clause always compared against `category.peer_stats.avg_ctr` regardless of which metric (`views`, `calls`, or `ctr`) was actually being reported — e.g. `"calls -50% down hai is week (peer median is 3%)"` implies the 3% is a calls benchmark, but it's a CTR level. Now the clause only attaches when `metric == "ctr"`, since that's the only benchmark `peer_stats` actually carries in a comparable unit.

### Why context payloads are plain `dict`, not typed Pydantic models

The testing brief's schemas are illustrative, not exhaustive — the real dataset already varies field-by-field (not every merchant has `review_themes`; `subscription` has different keys depending on `status`), and Phase 3 explicitly pushes *evolving* payloads. Pydantic models strict enough to validate the documented shape would reject legal partial payloads; models loose enough to accept everything would add no safety. Instead, every field read anywhere in the app goes through the defensive accessor `facts.g(d, *path, default=...)`, which never raises on a missing key — this is what lets the bot "genuinely read live context state at request time" instead of assuming the sample dataset's shape.

### Suppression, conversation state, and the hard constraints

- `ContextStore` is keyed by `(scope, context_id)`, versioned; a strictly-lower version is rejected with `409 stale_version`, an equal version is a no-op `200`, a higher version replaces atomically. Payloads over 500KB are rejected with `400` before they're even parsed.
- `SuppressionStore` tracks two things: per-`suppression_key` expiry (set to the trigger's own `expires_at` when we send), and a separate per-merchant opt-out flag (30 days) set the moment a merchant says "stop messaging me" — this suppresses *every* future trigger for that merchant, not just the current conversation.
- `ConversationStore` tracks turn history, every body already sent (for anti-repetition), and each merchant's last-touch timestamp, used to soft-deprioritize (not hard-block, except for very high urgency) sending to a merchant who was just messaged minutes ago and hasn't replied yet.
- `/v1/tick` enforces the 20-action cap, one action per `(merchant_id[, customer_id])` per tick, and a soft internal time budget (8s) — if a batch of triggers is unusually large, it returns whatever it finished composing rather than risk the judge's 30s timeout.
- `/v1/reply` never lets an unhandled exception surface as invalid JSON — both endpoints are wrapped so a bug degrades to `{"actions": []}` or a safe `wait`, never a malformed response or a 500.
- **Consistent 4xx envelope.** Both `/v1/context` and `/v1/reply` parse the raw request body themselves (`Request`, not a typed Pydantic parameter) and validate required fields by hand, so a malformed request — a missing field, a wrong type — always comes back as `400 {"accepted": false, "reason": "<code>", "details": "<what was wrong>"}`. Neither endpoint ever lets FastAPI's own validation layer fire and return its raw `{"detail": [...]}` 422 shape. (`/v1/tick` keeps a typed `body: TickRequest` parameter deliberately — a malformed tick request has no well-formed "no-op" reply the way context/reply do, and the judge harness's own schemas guarantee tick requests are well-formed, so the extra hand-validation isn't buying anything there.)
- **`from_role` is a closed set, not free text: `{"merchant", "customer"}`.** Nothing in the reply state machine currently branches on who sent the message — the same opt-out/hostile/deferral/intent patterns apply whether the merchant or the customer said them — so treating it as decoration would have been an easy accident. Instead it's validated explicitly: any other value (including a garbage string, a number after JSON parsing, or a missing key) is rejected with `400 {"accepted": false, "reason": "invalid_from_role", ...}`, the same way an invalid `scope` is rejected on `/v1/context`. This was a deliberate choice over "accept anything and treat it as a generic participant" — a `from_role` outside the two real participants in this system is far more likely to be a caller bug than a legitimate new kind of sender, and a loud, cheap-to-fix 400 beats silently accepting it into a field that (today) does nothing.

### `/v1/reply` state machine (`conversation.py`)

Priority order, each one a hard `return` (never falls through to a later rule):

1. **Hostile / explicit opt-out** — always ends immediately and suppresses the merchant for 30 days.
2. **Auto-reply / canned-text detection** — first sighting of a known canned phrase *or* the first exact repeat of the previous incoming message gets one gentle nudge (`send`); a second consecutive identical message backs off with `wait` (24h); a third `end`s the conversation. This exact three-step shape matches the testing brief's own "auto-reply hell" replay example.
3. **Deferral** ("give me time", "call you back") → `wait`.
4. **Explicit action-intent** ("let's do it", "go ahead", a bare "yes" with no question) → switches straight into an action-move template naming a concrete next step for that trigger's kind, **never** re-asking a qualifying question — this is the specific failure mode ("Intent-handoff failure") called out as production Vera's biggest miss.
5. **Off-topic curveball** — a deliberately narrow, explicit list of unrelated-domain markers (GST filing, insurance, legal advice, "can you also help me with…") triggers a one-line polite decline + redirect back to the original trigger. This is intentionally conservative: a genuine follow-up *question about the trigger itself* is not flagged as off-topic (that would be worse than not detecting real curveballs at all) — it falls through to the default acknowledgment instead.
6. **Turn cap** (5 turns) reached without resolution → `end` rather than looping forever.
7. **Default** — acknowledges and proposes the same concrete next step, rotating through 2–3 phrasing variants so turn 3's message is never identical to turn 2's.

Every `send` from this state machine is checked against `sent_bodies` for that conversation and never repeats verbatim (falls back to an explicit "circling back" variant as a last resort if every variant has been used).

---

## Model / approach choice

**No LLM in the composition path — deterministic templates only**, dispatched by `(category, trigger.kind)` and filled exclusively with facts the decision layer verified against source context. This is a stronger fit for this specific challenge than an LLM-based composer, for reasons specific to *this* rubric:

- The spec's own non-negotiable design principle is "decide with rules, phrase with templates" and an explicit anti-hallucination validator with **template fallback** — i.e., the brief itself specifies the deterministic architecture as the target, not merely an acceptable one.
- Zero hallucination risk by construction: the composer physically cannot reference a fact it wasn't handed, and the validator is a second, independent check.
- Sub-millisecond composition — no risk of ever approaching the 30s budget, no dependency on an external API's uptime/rate limits/cost during a scored 60-minute window.
- Fully reproducible for the determinism requirement — the same input always produces the byte-identical output (tested in `tests/test_determinism.py`), which an LLM at temperature 0 usually but not always guarantees across providers/versions.

## Tradeoffs

- **Ceiling on nuance.** A frontier LLM composer can react to genuinely novel phrasing or a subtle merchant question with real comprehension; this engine's `/v1/reply` state machine is keyword/pattern-based and will occasionally misclassify an ambiguous message (e.g. a genuinely curious question that also happens to contain a deferral word). The off-topic and action-intent detectors are deliberately tuned conservative (prefer a safe default acknowledgment over a wrong classification) — see `conversation.py`'s docstring for the exact priority order and reasoning.
- **New trigger `kind`s** the engine has never seen get the generic fallback template, not a bespoke one — correct (never fabricates), but scores lower on specificity than a bespoke template would if the payload actually contains rich, unfamiliar-but-parseable data. Extending coverage is pure data work: add one function to `facts.py`'s extractor registry and one to `composer.py`'s renderer registry.
- **Regional-language code-mixes we don't fabricate.** `language.py` produces English or Hindi-English (Hinglish) only. For a customer whose `language_pref` is `te-en mix` / `kn-en mix` / `ta-en mix` / a plain regional code, we deliberately stay in English rather than generate Telugu/Kannada/Tamil/Marathi text we have no verified vocabulary for — inventing words in a language we can't actually speak is a worse failure than an honest English fallback. We still honor the *relationship* (first name, warmth, real facts); we just don't fabricate the *language*.
- **The double-text cooldown is a heuristic, not a hard guarantee.** A merchant messaged 10 minutes ago via a lower-scoring trigger can still get a second message this tick if a *much* higher-urgency trigger (urgency 5) fires for them — deliberate (a supply-chain recall shouldn't wait behind cooldown logic), but it is a design choice that trades a small spam risk for never missing something genuinely urgent.

## What additional context would have helped most

1. **A real WhatsApp Business template library** (the actual Kaleyra-approved templates) — right now `template_name`/`template_params` are synthesized plausibly (`vera_{kind}_v1`, `[merchant_name, why_now, body]`) but a real template catalog would let the first-touch-per-24h-window distinction be enforced structurally rather than assumed.
2. **A ground-truth mapping from trigger `kind` → expected specificity** (which fields of the payload the judge expects cited) would remove the guesswork in `facts.py`'s ~30 per-kind extractors, several of which (e.g. `local_news_event`, `weather_heatwave`) had to guess at payload field names since no seed example exercises them.
3. **A larger set of scored example replies** (beyond the 3 replay scenarios) for calibrating the `/v1/reply` state machine's edge cases — e.g. what the "right" answer looks like when a merchant asks a *genuine* clarifying question mid-pitch, which this build currently treats as a default acknowledgment rather than attempting deterministic Q&A.

---

## Local test suite

`tests/` covers exactly what the build spec's step 10 asked for:

| File | Covers |
|---|---|
| `tests/test_context.py` | Idempotent posting, the `409 stale_version` case, version-bump replace, `400` for invalid scope / oversized payload, teardown |
| `tests/test_tick.py` | Specific, sourced composition for a merchant- and a customer-facing trigger; customer-context-not-yet-arrived is skipped (never fabricated); active suppression blocks a resend; 20-action cap; one action per merchant per tick; no URLs / no `None` leaks ever |
| `tests/test_reply.py` | Accept / decline / hostile+opt-out-suppression / off-topic-stays-on-mission / the full 4-turn auto-reply-hell sequence / intent-transition-skips-qualifying / never-repeats-a-body / turn-cap |
| `tests/test_determinism.py` | Same `/v1/tick` and `/v1/reply` input twice (fresh conversation/suppression state, same context) → byte-identical output |

`scripts/smoke_test.py` is a non-LLM structural pass over the **entire** 100-trigger expanded dataset — useful for eyeballing every composed message's quality in one run before spending judge-simulator LLM calls.

---

## Deployment (Render, free tier)

1. Push this directory to a GitHub repo (`bot.py`, `app/`, `requirements.txt` at minimum).
2. On [render.com](https://render.com) → **New +** → **Web Service** → connect the repo.
3. Settings:
   - **Environment**: `Python 3`
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `uvicorn bot:app --host 0.0.0.0 --port $PORT`
   - **Plan**: Free
4. Render gives you a public URL like `https://<service-name>.onrender.com` — that's what you submit. Confirm `GET /v1/healthz` responds before submitting.

Free tier spins down after ~15 minutes idle and cold-starts on the next request (a few seconds) — fine for the warmup phase (judge calls `/v1/healthz` first and waits), but if you want zero cold-start risk during the scored window, upgrade to the $7/mo Starter tier, or use Railway/Fly.io instead (steps below are nearly identical: point the start command at `uvicorn bot:app --host 0.0.0.0 --port $PORT` and both platforms auto-detect the Python build).

### Railway

```bash
railway init
railway up
```
Set the start command in `railway.json` / the dashboard to `uvicorn bot:app --host 0.0.0.0 --port $PORT`. Railway's free tier doesn't spin down, which is preferable for this kind of always-on test window.

### Fly.io

```bash
fly launch    # accept the auto-detected Python/uvicorn config, or point it at bot:app
fly deploy
```

A `Procfile` (`web: uvicorn bot:app --host 0.0.0.0 --port $PORT`) is included for any platform that reads one (Railway, Heroku-style buildpacks).

---

## Endpoint reference

See `challenge-testing-brief.md` in the original challenge zip for the full contract this implements. Summary:

| Endpoint | Method | Notes |
|---|---|---|
| `/v1/context` | POST | `{scope, context_id, version, payload, delivered_at}` → `200`/`409`/`400 {"accepted": false, "reason", "details"}` |
| `/v1/tick` | POST | `{now, available_triggers}` → `{actions: [...]}`, max 20, may be empty |
| `/v1/reply` | POST | `{conversation_id, merchant_id, customer_id, from_role, message, received_at, turn_number}` → `send`/`wait`/`end`, or `400 {"accepted": false, "reason", "details"}` if `message`/`from_role`/etc. are missing or invalid — see "Suppression, conversation state, and the hard constraints" above for the exact rules |
| `/v1/healthz` | GET | `{status, uptime_seconds, contexts_loaded}` |
| `/v1/metadata` | GET | team/model/approach metadata |
| `/v1/teardown` | POST | wipes all in-memory state (optional, called at test end) |
