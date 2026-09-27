#!/usr/bin/env python3
"""Local, non-LLM smoke test — exercises every endpoint against the full
expanded dataset and prints every composed message body so a human can
eyeball quality before wiring up judge_simulator.py (which needs an LLM key
for scoring). This does NOT score messages; it verifies the service doesn't
crash, respects the hard constraints, and produces plausible output.

Run with:
    python scripts/smoke_test.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.main import app

DATASET = Path(__file__).resolve().parent.parent / "dataset" / "expanded"


def load_all():
    categories = {}
    for f in (DATASET / "categories").glob("*.json"):
        data = json.load(open(f))
        categories[data["slug"]] = data
    merchants = {}
    for f in (DATASET / "merchants").glob("*.json"):
        data = json.load(open(f))
        merchants[data["merchant_id"]] = data
    customers = {}
    for f in (DATASET / "customers").glob("*.json"):
        data = json.load(open(f))
        customers[data["customer_id"]] = data
    triggers = {}
    for f in (DATASET / "triggers").glob("*.json"):
        data = json.load(open(f))
        triggers[data["id"]] = data
    return categories, merchants, customers, triggers


def main() -> int:
    client = TestClient(app)
    categories, merchants, customers, triggers = load_all()
    print(f"Loaded {len(categories)} categories, {len(merchants)} merchants, {len(customers)} customers, {len(triggers)} triggers")

    r = client.get("/v1/healthz")
    assert r.status_code == 200 and r.json()["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    print("[PASS] healthz starts at zero")

    v = 1
    for slug, payload in categories.items():
        r = client.post("/v1/context", json={"scope": "category", "context_id": slug, "version": v, "payload": payload, "delivered_at": "2026-04-26T09:00:00Z"})
        assert r.status_code == 200, r.text
    for mid, payload in merchants.items():
        r = client.post("/v1/context", json={"scope": "merchant", "context_id": mid, "version": v, "payload": payload, "delivered_at": "2026-04-26T09:00:00Z"})
        assert r.status_code == 200, r.text
    for cid, payload in customers.items():
        r = client.post("/v1/context", json={"scope": "customer", "context_id": cid, "version": v, "payload": payload, "delivered_at": "2026-04-26T09:00:00Z"})
        assert r.status_code == 200, r.text
    print("[PASS] pushed full base dataset (0 triggers, per warmup phase 1)")

    first_mid = next(iter(merchants))
    first_payload = merchants[first_mid]
    r = client.post("/v1/context", json={"scope": "merchant", "context_id": first_mid, "version": v, "payload": first_payload, "delivered_at": "2026-04-26T09:00:00Z"})
    assert r.status_code == 200, r.text
    print("[PASS] idempotency: re-posting same version -> 200 no-op")

    r = client.post("/v1/context", json={"scope": "merchant", "context_id": first_mid, "version": 2, "payload": first_payload, "delivered_at": "2026-04-26T09:05:00Z"})
    assert r.status_code == 200, r.text
    r = client.post("/v1/context", json={"scope": "merchant", "context_id": first_mid, "version": 1, "payload": first_payload, "delivered_at": "2026-04-26T09:00:00Z"})
    assert r.status_code == 409, r.text
    assert r.json()["current_version"] == 2, r.text
    print("[PASS] posting a lower version after a bump -> 409 stale_version")

    r = client.get("/v1/healthz")
    counts = r.json()["contexts_loaded"]
    assert counts == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}, counts
    print(f"[PASS] healthz after warmup: {counts}")

    # Push all triggers and tick in batches, exercising the full decision + composer + validator path.
    all_trigger_ids = list(triggers.keys())
    total_actions = 0
    seen_bodies_per_conv = {}
    for i in range(0, len(all_trigger_ids), 10):
        batch = all_trigger_ids[i : i + 10]
        for tid in batch:
            r = client.post("/v1/context", json={"scope": "trigger", "context_id": tid, "version": 1, "payload": triggers[tid], "delivered_at": "2026-04-26T10:30:00Z"})
            assert r.status_code == 200, r.text
        r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": batch})
        assert r.status_code == 200, r.text
        actions = r.json()["actions"]
        assert len(actions) <= 20
        for a in actions:
            required = {"conversation_id", "merchant_id", "send_as", "trigger_id", "template_name", "template_params", "body", "cta", "suppression_key", "rationale"}
            assert required.issubset(a.keys()), a
            assert a["body"].strip(), "empty body"
            assert "http://" not in a["body"] and "https://" not in a["body"] and "www." not in a["body"], a["body"]
            total_actions += 1
            print(f"  [{a['send_as']:16}] ({a['trigger_id']:38}) {a['cta']:20} {a['body'][:110]}")

    print(f"\n[INFO] total actions across all {len(all_trigger_ids)} triggers: {total_actions}")

    # Replay-style scenarios (no LLM needed — these are literal string checks).
    mid = next(iter(merchants))
    print("\n--- auto-reply hell ---")
    canned = "Thank you for contacting us! Our team will respond shortly."
    for i in range(1, 5):
        r = client.post("/v1/reply", json={"conversation_id": "conv_autoreply_smoke", "merchant_id": mid, "from_role": "merchant", "message": canned, "received_at": "2026-04-26T10:40:00Z", "turn_number": i + 1})
        data = r.json()
        print(f"  turn {i+1}: {data.get('action')} — {data.get('rationale', '')[:90]}")
        if data.get("action") == "end":
            break

    print("\n--- intent transition ---")
    r = client.post("/v1/reply", json={"conversation_id": "conv_intent_smoke", "merchant_id": mid, "from_role": "merchant", "message": "Ok lets do it. Whats next?", "received_at": "2026-04-26T10:40:00Z", "turn_number": 2})
    print(" ", r.json())

    print("\n--- hostile ---")
    r = client.post("/v1/reply", json={"conversation_id": "conv_hostile_smoke", "merchant_id": mid, "from_role": "merchant", "message": "Stop messaging me. This is useless spam.", "received_at": "2026-04-26T10:40:00Z", "turn_number": 2})
    print(" ", r.json())

    print("\n--- off-topic curveball ---")
    r = client.post("/v1/reply", json={"conversation_id": "conv_curveball_smoke", "merchant_id": mid, "from_role": "merchant", "message": "Btw can you also help me file my GST this month?", "received_at": "2026-04-26T10:40:00Z", "turn_number": 2})
    print(" ", r.json())

    print("\n--- teardown ---")
    r = client.post("/v1/teardown")
    assert r.status_code == 200
    r = client.get("/v1/healthz")
    assert r.json()["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    print("[PASS] teardown wipes all state")

    print("\nALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
