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


# --------------------------------------------------------------------------- #
# POST /normalize-document
# --------------------------------------------------------------------------- #

SRT_DOC = (
    "1\n"
    "00:00:01,000 --> 00:00:02,000\n"
    "你好\n"
    "\n"
    "2\n"
    "00:00:01,500 --> 00:00:03,000\n"
    "世界\n"
)

WEBVTT_DOC = (
    "WEBVTT\n"
    "\n"
    "NOTE 片头注释\n"
    "\n"
    "intro\n"
    "00:00.000 --> 00:01.000 align:start\n"
    "你好\n"
    "\n"
    "00:00.500 --> 00:02.000\n"
    "世界\n"
)


def test_normalize_document_srt():
    resp = client.post(
        "/normalize-document",
        json={"format": "srt", "content": SRT_DOC},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["format"] == "srt"
    assert body["cue_count"] == 2
    assert body["min_gap_ms"] == 0
    assert body["total_absolute_displacement_ms"] == 500
    assert body["normalized_content"] == (
        "1\n"
        "00:00:00,500 --> 00:00:01,500\n"
        "你好\n"
        "\n"
        "2\n"
        "00:00:01,500 --> 00:00:03,000\n"
        "世界\n"
    )


def test_normalize_document_srt_crlf_kept():
    resp = client.post(
        "/normalize-document",
        json={"format": "srt", "content": SRT_DOC.replace("\n", "\r\n")},
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()["normalized_content"]
    assert "\r\n" in out
    assert "\n" not in out.replace("\r\n", "")
    assert "\r" not in out.replace("\r\n", "")


def test_normalize_document_webvtt_preserves_non_cue_content():
    resp = client.post(
        "/normalize-document",
        json={"format": "webvtt", "content": WEBVTT_DOC, "pinned_indices": [0]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cue_count"] == 2
    assert body["total_absolute_displacement_ms"] == 500
    assert body["normalized_content"] == WEBVTT_DOC.replace(
        "00:00.500 --> 00:02.000", "00:01.000 --> 00:02.500"
    )


def test_normalize_document_min_gap_applies():
    resp = client.post(
        "/normalize-document",
        json={"format": "srt", "content": SRT_DOC, "min_gap_ms": 100},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["min_gap_ms"] == 100
    # Cue 0 moves to [400, 1400], cue 1 keeps [1500, 3000]: gap of 100 kept.
    assert "00:00:00,400 --> 00:00:01,400" in body["normalized_content"]
    assert "00:00:01,500 --> 00:00:03,000" in body["normalized_content"]
    assert body["total_absolute_displacement_ms"] == 600


def test_normalize_document_empty_srt():
    resp = client.post(
        "/normalize-document",
        json={"format": "srt", "content": ""},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cue_count"] == 0
    assert body["normalized_content"] == ""
    assert body["total_absolute_displacement_ms"] == 0


def test_normalize_document_conflict_returns_409():
    resp = client.post(
        "/normalize-document",
        json={
            "format": "srt",
            "content": (
                "1\n00:00:00,000 --> 00:00:01,000\nA\n\n"
                "2\n00:00:00,500 --> 00:00:01,500\nB\n"
            ),
            "pinned_indices": [0, 1],
        },
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "infeasible_constraints"
    assert len(body["conflicts"]) == 1
    c = body["conflicts"][0]
    assert c["from_index"] == 0
    assert c["from_id"] == "1"  # SRT sequence numbers are echoed as ids
    assert c["to_index"] == 1
    assert c["to_id"] == "2"
    assert c["required_start_ms"] == 1000
    assert c["actual_start_ms"] == 500
    assert c["shortfall_ms"] == 500


def test_normalize_document_syntax_error_422_with_line():
    content = (
        "1\n00:00:01,000 --> 00:00:02,000\nA\n\n"
        "2\nthis is not a timing line\nB\n"
    )
    resp = client.post(
        "/normalize-document",
        json={"format": "srt", "content": content},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "document_syntax_error"
    assert body["line"] == 6
    assert "timing line" in body["message"]


def test_normalize_document_webvtt_missing_header_422():
    resp = client.post(
        "/normalize-document",
        json={"format": "webvtt", "content": "00:00.000 --> 00:01.000\nA\n"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "document_syntax_error"
    assert body["line"] == 1


def test_normalize_document_pinned_index_out_of_range_422():
    resp = client.post(
        "/normalize-document",
        json={"format": "srt", "content": SRT_DOC, "pinned_indices": [5]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "invalid_pinned_indices"
    assert "5" in body["message"]


def test_normalize_document_bad_format_422():
    resp = client.post(
        "/normalize-document",
        json={"format": "ass", "content": ""},
    )
    assert resp.status_code == 422


def test_normalize_document_negative_pinned_index_422():
    resp = client.post(
        "/normalize-document",
        json={"format": "srt", "content": SRT_DOC, "pinned_indices": [-1]},
    )
    assert resp.status_code == 422


def test_normalize_document_negative_gap_422():
    resp = client.post(
        "/normalize-document",
        json={"format": "srt", "content": SRT_DOC, "min_gap_ms": -1},
    )
    assert resp.status_code == 422
