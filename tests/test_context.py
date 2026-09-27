import json

from tests.conftest import push


def test_healthz_starts_empty(client):
    r = client.get("/v1/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}


def test_metadata_shape(client):
    r = client.get("/v1/metadata")
    assert r.status_code == 200
    body = r.json()
    for field in ("team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"):
        assert field in body


def test_push_context_accepted(client, dentists_category):
    r = push(client, "category", "dentists", dentists_category, version=1)
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["ack_id"]
    assert body["stored_at"]


def test_push_same_version_is_idempotent_noop(client, dentists_category):
    r1 = push(client, "category", "dentists", dentists_category, version=1)
    r2 = push(client, "category", "dentists", dentists_category, version=1)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json()["accepted"] is True


def test_lower_version_rejected_with_409(client, dentists_category):
    push(client, "category", "dentists", dentists_category, version=3)
    r = push(client, "category", "dentists", dentists_category, version=2)
    assert r.status_code == 409
    body = r.json()
    assert body["accepted"] is False
    assert body["reason"] == "stale_version"
    assert body["current_version"] == 3


def test_higher_version_replaces_atomically(client, dr_meera):
    push(client, "merchant", dr_meera["merchant_id"], dr_meera, version=1)
    updated = dict(dr_meera)
    updated["performance"] = dict(dr_meera["performance"])
    updated["performance"]["views"] = 99999
    r = push(client, "merchant", dr_meera["merchant_id"], updated, version=2)
    assert r.status_code == 200

    from app.main import contexts

    stored = contexts.get("merchant", dr_meera["merchant_id"])
    assert stored["performance"]["views"] == 99999


def test_invalid_scope_returns_400(client):
    r = client.post(
        "/v1/context",
        json={"scope": "not_a_real_scope", "context_id": "x", "version": 1, "payload": {}, "delivered_at": "2026-04-26T09:00:00Z"},
    )
    assert r.status_code == 400
    assert r.json()["accepted"] is False
    assert r.json()["reason"] == "invalid_scope"


def test_payload_over_500kb_rejected(client):
    huge_payload = {"blob": "x" * (600 * 1024)}
    body = json.dumps(
        {"scope": "merchant", "context_id": "m_huge", "version": 1, "payload": huge_payload, "delivered_at": "2026-04-26T09:00:00Z"}
    )
    r = client.post("/v1/context", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["accepted"] is False


def test_teardown_wipes_all_state(client, dentists_category, dr_meera):
    push(client, "category", "dentists", dentists_category)
    push(client, "merchant", dr_meera["merchant_id"], dr_meera)
    r = client.post("/v1/teardown")
    assert r.status_code == 200
    counts = client.get("/v1/healthz").json()["contexts_loaded"]
    assert counts == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
