"""Same input twice -> identical output. Required by the build spec and by
the composition contract ("must be deterministic given the same inputs").
"""

from tests.conftest import push


def test_tick_is_deterministic_across_repeated_runs(client, dentists_category, dr_meera, research_digest_trigger):
    push(client, "category", "dentists", dentists_category)
    push(client, "merchant", dr_meera["merchant_id"], dr_meera)
    push(client, "trigger", research_digest_trigger["id"], research_digest_trigger)

    r1 = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": [research_digest_trigger["id"]]})
    a1 = r1.json()["actions"][0]

    # Reset only the conversation/suppression state (not context) and re-run
    # the exact same tick — the composed body/cta/rationale must match
    # byte-for-byte since nothing about the inputs changed.
    from app.main import conversations, suppressions

    conversations.clear()
    suppressions.clear()

    r2 = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": [research_digest_trigger["id"]]})
    a2 = r2.json()["actions"][0]

    assert a1["body"] == a2["body"]
    assert a1["cta"] == a2["cta"]
    assert a1["send_as"] == a2["send_as"]
    assert a1["rationale"] == a2["rationale"]
    assert a1["suppression_key"] == a2["suppression_key"]
    assert a1["conversation_id"] == a2["conversation_id"]


def test_reply_classification_is_deterministic(client, dentists_category, dr_meera, research_digest_trigger):
    push(client, "category", "dentists", dentists_category)
    push(client, "merchant", dr_meera["merchant_id"], dr_meera)
    push(client, "trigger", research_digest_trigger["id"], research_digest_trigger)
    r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": [research_digest_trigger["id"]]})
    conv_id_1 = r.json()["actions"][0]["conversation_id"]

    reply1 = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id_1,
            "merchant_id": dr_meera["merchant_id"],
            "customer_id": None,
            "from_role": "merchant",
            "message": "Ok lets do it",
            "received_at": "2026-04-26T10:45:00Z",
            "turn_number": 2,
        },
    ).json()

    # Re-run the exact same scenario from a clean conversation store.
    from app.main import conversations, suppressions

    conversations.clear()
    suppressions.clear()
    r2 = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": [research_digest_trigger["id"]]})
    conv_id_2 = r2.json()["actions"][0]["conversation_id"]
    assert conv_id_2 == conv_id_1

    reply2 = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id_2,
            "merchant_id": dr_meera["merchant_id"],
            "customer_id": None,
            "from_role": "merchant",
            "message": "Ok lets do it",
            "received_at": "2026-04-26T10:45:00Z",
            "turn_number": 2,
        },
    ).json()

    assert reply1["action"] == reply2["action"]
    assert reply1.get("body") == reply2.get("body")
    assert reply1.get("cta") == reply2.get("cta")
