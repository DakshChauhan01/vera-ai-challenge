from tests.conftest import push


def _seed_conversation(client, dentists_category, dr_meera, research_digest_trigger, conv_id="conv_reply_test"):
    push(client, "category", "dentists", dentists_category)
    push(client, "merchant", dr_meera["merchant_id"], dr_meera)
    push(client, "trigger", research_digest_trigger["id"], research_digest_trigger)
    r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": [research_digest_trigger["id"]]})
    action = r.json()["actions"][0]
    return action["conversation_id"]


def _reply(client, conv_id, merchant_id, message, turn_number):
    return client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": None,
            "from_role": "merchant",
            "message": message,
            "received_at": "2026-04-26T10:45:00Z",
            "turn_number": turn_number,
        },
    )


def test_reply_accept_gets_action_response(client, dentists_category, dr_meera, research_digest_trigger):
    conv_id = _seed_conversation(client, dentists_category, dr_meera, research_digest_trigger)
    r = _reply(client, conv_id, dr_meera["merchant_id"], "Yes please send the abstract", 2)
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "send"
    assert body["body"].strip()
    assert "rationale" in body


def test_reply_decline_ends_gracefully(client, dentists_category, dr_meera, research_digest_trigger):
    conv_id = _seed_conversation(client, dentists_category, dr_meera, research_digest_trigger)
    r = _reply(client, conv_id, dr_meera["merchant_id"], "Not interested. Stop messaging me.", 2)
    assert r.status_code == 200
    assert r.json()["action"] == "end"


def test_reply_hostile_ends_and_suppresses_merchant(client, dentists_category, dr_meera, research_digest_trigger):
    conv_id = _seed_conversation(client, dentists_category, dr_meera, research_digest_trigger)
    r = _reply(client, conv_id, dr_meera["merchant_id"], "Why are you bothering me. This is useless. Stop sending these.", 2)
    assert r.json()["action"] == "end"

    from app.main import suppressions
    import time

    assert suppressions.is_merchant_opted_out(dr_meera["merchant_id"], time.time())


def test_reply_off_topic_stays_on_mission(client, dentists_category, dr_meera, research_digest_trigger):
    conv_id = _seed_conversation(client, dentists_category, dr_meera, research_digest_trigger)
    r = _reply(client, conv_id, dr_meera["merchant_id"], "Btw can you also help me file my GST this month?", 2)
    body = r.json()
    assert body["action"] == "send"
    assert "gst" not in body["body"].lower() or "outside" in body["body"].lower() or "scope" in body["body"].lower() or "bahar" in body["body"].lower()


def test_reply_auto_reply_hell_sequence(client, dentists_category, dr_meera, research_digest_trigger):
    conv_id = _seed_conversation(client, dentists_category, dr_meera, research_digest_trigger)
    canned = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."

    r2 = _reply(client, conv_id, dr_meera["merchant_id"], canned, 2)
    assert r2.json()["action"] == "send"  # first sighting: one nudge

    r3 = _reply(client, conv_id, dr_meera["merchant_id"], canned, 3)
    assert r3.json()["action"] == "wait"  # repeated once: back off

    r4 = _reply(client, conv_id, dr_meera["merchant_id"], canned, 4)
    assert r4.json()["action"] == "end"  # repeated again: give up gracefully


def test_reply_intent_transition_switches_to_action_not_qualifying(client, dentists_category, dr_meera, research_digest_trigger):
    conv_id = _seed_conversation(client, dentists_category, dr_meera, research_digest_trigger)
    r = _reply(client, conv_id, dr_meera["merchant_id"], "Ok lets do it. Whats next?", 2)
    body = r.json()
    assert body["action"] == "send"
    lowered = body["body"].lower()
    qualifying_markers = ["would you", "do you think", "would that work", "should i"]
    assert not any(m in lowered for m in qualifying_markers)


def test_reply_never_repeats_a_body_verbatim_in_one_conversation(client, dentists_category, dr_meera, research_digest_trigger):
    conv_id = _seed_conversation(client, dentists_category, dr_meera, research_digest_trigger)
    bodies = []
    for i, msg in enumerate(["Tell me more", "What else", "Anything else I should know"], start=2):
        r = _reply(client, conv_id, dr_meera["merchant_id"], msg, i)
        data = r.json()
        if data["action"] == "send":
            bodies.append(data["body"])
    assert len(bodies) == len(set(bodies)), f"duplicate body sent: {bodies}"


def test_reply_max_turns_ends_conversation(client, dentists_category, dr_meera, research_digest_trigger):
    conv_id = _seed_conversation(client, dentists_category, dr_meera, research_digest_trigger)
    filler_messages = [
        "hmm ok interesting",
        "tell me more about that",
        "what else is in there",
        "anything else I should know",
        "one more question about this",
        "still thinking about it",
    ]
    last = None
    for turn, msg in zip(range(2, 8), filler_messages):
        last = _reply(client, conv_id, dr_meera["merchant_id"], msg, turn)
    assert last.json()["action"] == "end"
