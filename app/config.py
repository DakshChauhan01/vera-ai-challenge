"""Hard constraints from the challenge testing brief, centralized so behaviour
is easy to audit and tune in one place.
"""

# --- Payload / rate limits -------------------------------------------------
CONTEXT_PAYLOAD_CAP_BYTES = 500 * 1024  # 500 KB, testing-brief §5
MAX_ACTIONS_PER_TICK = 20  # testing-brief §5 / §2.2

# --- Response budgets -------------------------------------------------------
# The judge gives us 30s per call; we budget well under that so we always
# return before the judge's own timeout fires, per testing-brief §5 / FAQ:
# "If your bot needs more than 30s ... return {'actions': []} immediately".
TICK_SOFT_BUDGET_SECONDS = 8.0
REPLY_SOFT_BUDGET_SECONDS = 5.0

# --- Conversation / suppression behaviour -----------------------------------
# How long a merchant who explicitly opts out ("stop messaging me") is
# suppressed for, across every trigger and conversation.
MERCHANT_OPT_OUT_SUPPRESS_SECONDS = 30 * 24 * 3600  # 30 days

# Auto-reply handling (see app/conversation.py):
#   1st time a message looks like a canned auto-reply -> send one nudge
#   2nd consecutive identical occurrence                -> wait
#   3rd+ consecutive identical occurrence                -> end
AUTO_REPLY_WAIT_SECONDS = 24 * 3600  # 24h, matches testing-brief example 4.1

# Generic "give me time" deferral window when a merchant asks to be
# revisited later without giving a specific time.
DEFAULT_WAIT_SECONDS = 1800  # 30 minutes

# Avoid double-texting the same merchant within this window even if a
# second, unrelated trigger scores higher in the same tick — merchants who
# were messaged very recently (by Vera, unanswered) are deprioritized
# rather than hard-blocked, see app/decision_engine.py.
RECENT_TOUCH_SOFT_COOLDOWN_SECONDS = 4 * 3600  # 4 hours

# Hard ceiling on turns per conversation before we wind things down even if
# intent is ambiguous (Phase 2 / Phase 4 both cap at 5 turns).
MAX_CONVERSATION_TURNS = 5
