"""HTTP-level tests for the FastAPI app."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_root_links_docs():
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["docs"] == "/docs"


def test_normalize_feasible():
    resp = client.post(
        "/normalize",
        json={
            "min_gap_ms": 0,
            "segments": [
                {"id": "s1", "text": "a", "start_ms": 0, "end_ms": 1000},
                {"id": "s2", "text": "b", "start_ms": 500, "end_ms": 1500},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["min_gap_ms"] == 0
    starts = [s["start_ms"] for s in body["segments"]]
    assert starts[1] >= starts[0] + 1000
    for seg in body["segments"]:
        assert seg["end_ms"] - seg["start_ms"] == 1000
    assert body["segments"][0]["text"] == "a"
    assert body["segments"][0]["id"] == "s1"
    assert body["total_absolute_displacement_ms"] == sum(
        s["displacement_ms"] for s in body["segments"]
    )


def test_normalize_min_gap_and_pins():
    resp = client.post(
        "/normalize",
        json={
            "min_gap_ms": 100,
            "segments": [
                {"text": "a", "start_ms": 0, "end_ms": 500, "pinned": True},
                {"text": "b", "start_ms": 200, "end_ms": 700},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    segs = resp.json()["segments"]
    assert segs[0]["start_ms"] == 0  # locked
    assert segs[1]["start_ms"] == 600  # 500 duration + 100 gap


def test_normalize_conflict_returns_409():
    resp = client.post(
        "/normalize",
        json={
            "segments": [
                {"id": "a", "text": "a", "start_ms": 0, "end_ms": 1000, "pinned": True},
                {"id": "b", "text": "b", "start_ms": 500, "end_ms": 1500, "pinned": True},
            ]
        },
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "infeasible_constraints"
    assert len(body["conflicts"]) == 1
    c = body["conflicts"][0]
    assert c["from_id"] == "a"
    assert c["to_id"] == "b"
    assert c["required_start_ms"] == 1000
    assert c["actual_start_ms"] == 500
    assert c["shortfall_ms"] == 500
    assert "cannot both keep" in c["reason"]


def test_normalize_empty_segments():
    resp = client.post("/normalize", json={"segments": []})
    assert resp.status_code == 200
    body = resp.json()
    assert body["segments"] == []
    assert body["total_absolute_displacement_ms"] == 0


def test_invalid_duration_rejected():
    resp = client.post(
        "/normalize",
        json={"segments": [{"text": "a", "start_ms": 100, "end_ms": 100}]},
    )
    assert resp.status_code == 422


def test_negative_start_rejected():
    resp = client.post(
        "/normalize",
        json={"segments": [{"text": "a", "start_ms": -1, "end_ms": 100}]},
    )
    assert resp.status_code == 422


def test_negative_gap_rejected():
    resp = client.post(
        "/normalize",
        json={"min_gap_ms": -5, "segments": []},
    )
    assert resp.status_code == 422


def test_extra_fields_ignored():
    resp = client.post(
        "/normalize",
        json={
            "segments": [
                {
                    "text": "a",
                    "start_ms": 0,
                    "end_ms": 100,
                    "style": "italic",
                    "speaker": "narrator",
                }
            ],
            "unexpected": "value",
        },
    )
    assert resp.status_code == 200, resp.text
