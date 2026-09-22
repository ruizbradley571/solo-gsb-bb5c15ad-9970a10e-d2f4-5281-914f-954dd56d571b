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
    "1\n00:00:00,000 --> 00:00:01,000\nHello\nworld\n\n"
    "2\n00:00:00,500 --> 00:00:02,000\nMiddle\n\n"
    "3\n00:00:00,600 --> 00:00:03,000\nBye\n"
)

VTT_DOC = (
    "WEBVTT - demo\n\n"
    "NOTE keep me\n\n"
    "REGION\nid:top\n\n"
    "cue-a\n00:00.000 --> 00:01.000 line:0\nHello\n\n"
    "00:00.500 --> 00:02.000\nMiddle\n"
)


def test_normalize_document_srt_overlaps_fixed():
    resp = client.post(
        "/normalize-document",
        json={"format": "srt", "content": SRT_DOC, "min_gap_ms": 0},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["format"] == "srt"
    assert body["cue_count"] == 3
    out = body["normalized_content"]
    # Numbers and multi-line text preserved verbatim ...
    assert out.startswith("1\n")
    assert "Hello\nworld\n" in out
    assert "\n2\n" in out and "\n3\n" in out
    assert out.endswith("Bye\n")
    # ... and cue 2 now starts no earlier than cue 1's end.
    assert "00:00:01,000 --> 00:00:02,500" in out
    assert "00:00:02,500 --> 00:00:04,900" in out
    # displacements: cue0 0, cue1 +500, cue2 +1900
    assert body["total_absolute_displacement_ms"] == 2400


def test_normalize_document_min_gap():
    resp = client.post(
        "/normalize-document",
        json={"format": "srt", "content": SRT_DOC, "min_gap_ms": 100},
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()["normalized_content"]
    assert "00:00:01,100 --> 00:00:02,600" in out
    assert "00:00:02,700 --> 00:00:05,100" in out


def test_normalize_document_pinned_indices():
    doc = (
        "1\n00:00:00,000 --> 00:00:01,000\nFirst\n\n"
        "2\n00:00:00,500 --> 00:00:02,000\nSecond\n"
    )
    resp = client.post(
        "/normalize-document",
        json={
            "format": "srt",
            "content": doc,
            "min_gap_ms": 0,
            "pinned_indices": [0],
        },
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()["normalized_content"]
    lines = {t for t in out.splitlines() if "-->" in t}
    assert "00:00:00,000 --> 00:00:01,000" in lines  # pinned cue unchanged
    assert "00:00:01,000 --> 00:00:02,500" in lines  # free cue moved after it
    # Identifiers and text untouched.
    assert out == (
        "1\n00:00:00,000 --> 00:00:01,000\nFirst\n\n"
        "2\n00:00:01,000 --> 00:00:02,500\nSecond\n"
    )


def test_normalize_document_pinned_conflict_returns_409():
    resp = client.post(
        "/normalize-document",
        json={
            "format": "srt",
            "content": SRT_DOC,
            "min_gap_ms": 0,
            "pinned_indices": [0, 1],
        },
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "infeasible_constraints"
    assert body["conflicts"][0]["from_index"] == 0
    assert body["conflicts"][0]["to_index"] == 1


def test_normalize_document_webvtt_preserves_envelope():
    resp = client.post(
        "/normalize-document",
        json={"format": "webvtt", "content": VTT_DOC, "min_gap_ms": 0},
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()["normalized_content"]
    assert out.startswith("WEBVTT - demo\n\nNOTE keep me\n\nREGION\nid:top\n\n")
    assert "cue-a\n" in out
    assert " line:0" in out  # timing-line settings preserved
    assert out.endswith("Middle") or out.endswith("Middle\n")
    # The locked-free overlap is repaired on the timing lines only.
    assert "00:01.000 --> 00:02.500" in out
    assert resp.json()["cue_count"] == 2


def test_normalize_document_webvtt_crlf_roundtrip_style():
    content = (
        "WEBVTT\r\n\r\n00:00:00.000 --> 00:00:01.000\r\na\r\n\r\n"
        "00:00:00.500 --> 00:00:02.000\r\nb\r\n"
    )
    resp = client.post(
        "/normalize-document",
        json={"format": "webvtt", "content": content, "min_gap_ms": 0},
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()["normalized_content"]
    assert "\r\n" in out and "\n" not in out.replace("\r\n", "")


def test_normalize_document_empty_documents():
    for fmt, content in (("srt", ""), ("webvtt", "WEBVTT\n")):
        resp = client.post(
            "/normalize-document",
            json={"format": fmt, "content": content, "min_gap_ms": 0},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cue_count"] == 0
        assert body["normalized_content"] == content
        assert body["total_absolute_displacement_ms"] == 0


def test_normalize_document_syntax_error_422_with_line():
    resp = client.post(
        "/normalize-document",
        json={
            "format": "srt",
            "content": "1\n00:00:00,000 --> 00:00:01,000\na\n\n2\nbroken timing\nb\n",
            "min_gap_ms": 0,
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "document_syntax_error"
    assert body["format"] == "srt"
    assert body["line"] == 6
    assert "line" not in body or isinstance(body["line"], int)


def test_normalize_document_webvtt_missing_header_422():
    resp = client.post(
        "/normalize-document",
        json={"format": "webvtt", "content": "00:00.000 --> 00:01.000\nx\n", "min_gap_ms": 0},
    )
    assert resp.status_code == 422
    assert resp.json()["line"] == 1
    assert resp.json()["error"] == "document_syntax_error"


def test_normalize_document_pinned_index_out_of_range_422():
    resp = client.post(
        "/normalize-document",
        json={
            "format": "srt",
            "content": SRT_DOC,
            "min_gap_ms": 0,
            "pinned_indices": [5],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_pinned_indices"


def test_normalize_document_bad_request_fields_422():
    # Unknown format / negative gap / negative or duplicate pinned index.
    assert (
        client.post(
            "/normalize-document",
            json={"format": "vtt", "content": "", "min_gap_ms": 0},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/normalize-document",
            json={"format": "srt", "content": "", "min_gap_ms": -1},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/normalize-document",
            json={"format": "srt", "content": "", "min_gap_ms": 0, "pinned_indices": [-1]},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/normalize-document",
            json={"format": "srt", "content": "", "min_gap_ms": 0, "pinned_indices": [0, 0]},
        ).status_code
        == 422
    )


def test_normalize_legacy_endpoint_still_compatible():
    resp = client.post(
        "/normalize",
        json={
            "min_gap_ms": 10,
            "segments": [
                {"text": "a", "start_ms": 0, "end_ms": 500, "pinned": True},
                {"text": "b", "start_ms": 200, "end_ms": 700},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"segments", "min_gap_ms", "total_absolute_displacement_ms"}
    assert body["segments"][0]["start_ms"] == 0
    assert body["segments"][1]["start_ms"] == 510
