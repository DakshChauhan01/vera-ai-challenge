import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATASET_DIR = ROOT / "dataset"


@pytest.fixture
def client():
    """Fresh app + fresh in-memory state per test (module-level singletons
    reset via /v1/teardown, since app.main constructs the stores once at
    import time)."""
    from app.main import app

    c = TestClient(app)
    c.post("/v1/teardown")
    yield c
    c.post("/v1/teardown")


@pytest.fixture(scope="session")
def dentists_category():
    return json.load(open(DATASET_DIR / "categories" / "dentists.json", encoding="utf-8"))


@pytest.fixture(scope="session")
def seed_merchants():
    return json.load(open(DATASET_DIR / "merchants_seed.json", encoding="utf-8"))["merchants"]


@pytest.fixture(scope="session")
def seed_customers():
    return json.load(open(DATASET_DIR / "customers_seed.json", encoding="utf-8"))["customers"]


@pytest.fixture(scope="session")
def seed_triggers():
    return json.load(open(DATASET_DIR / "triggers_seed.json", encoding="utf-8"))["triggers"]


@pytest.fixture(scope="session")
def dr_meera(seed_merchants):
    return next(m for m in seed_merchants if m["merchant_id"] == "m_001_drmeera_dentist_delhi")


@pytest.fixture(scope="session")
def priya(seed_customers):
    return next(c for c in seed_customers if c["customer_id"] == "c_001_priya_for_m001")


@pytest.fixture(scope="session")
def research_digest_trigger(seed_triggers):
    return next(t for t in seed_triggers if t["id"] == "trg_001_research_digest_dentists")


@pytest.fixture(scope="session")
def recall_due_trigger(seed_triggers):
    return next(t for t in seed_triggers if t["id"] == "trg_003_recall_due_priya")


def push(client, scope, context_id, payload, version=1, delivered_at="2026-04-26T09:00:00Z"):
    return client.post(
        "/v1/context",
        json={"scope": scope, "context_id": context_id, "version": version, "payload": payload, "delivered_at": delivered_at},
    )
