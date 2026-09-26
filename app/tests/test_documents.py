"""Unit tests for the SRT/WebVTT document parser and renderer."""

from __future__ import annotations

import pytest

from app.documents import (
    DocumentSyntaxError,
    TimestampStyle,
    format_timestamp,
    parse_document,
    parse_srt,
    parse_webvtt,
    render_document,
)
from app.solver import Cue, normalize


def normalize_doc(doc, min_gap_ms=0, pinned=()):
    """Run the solver over a parsed document and re-render it."""
    cues = [
        Cue(
            index=c.index,
            cue_id=c.identifier,
            text=c.text,
            start_ms=c.start_ms,
            end_ms=c.end_ms,
            pinned=c.index in pinned,
        )
        for c in doc.cues
    ]
    result = normalize(cues, min_gap_ms=min_gap_ms)
    return render_document(doc, result.cues), result


# --------------------------------------------------------------------------- #
# Timestamp formatting
# --------------------------------------------------------------------------- #


def test_format_timestamp_webvtt_short_form():
    assert format_timestamp(0, TimestampStyle(None, ".")) == "00:00.000"
    assert format_timestamp(61_500, TimestampStyle(None, ".")) == "01:01.500"
    # Hours are absorbed into the minutes field in short form.
    assert format_timestamp(3_661_234, TimestampStyle(None, ".")) == "61:01.234"


def test_format_timestamp_long_form_keeps_hour_width():
    assert format_timestamp(3_661_234, TimestampStyle(2, ",")) == "01:01:01,234"
    assert format_timestamp(500, TimestampStyle(1, ",")) == "0:00:00,500"
    # Values may grow beyond the original width when pushed far enough.
    assert format_timestamp(36_000_000, TimestampStyle(1, ",")) == "10:00:00,000"


# --------------------------------------------------------------------------- #
# SRT
# --------------------------------------------------------------------------- #

SRT_SAMPLE = (
    "1\n"
    "00:00:01,000 --> 00:00:02,000\n"
    "Hello\n"
    "\n"
    "2\n"
    "00:00:01,500 --> 00:00:03,000\n"
    "World\n"
)


def test_srt_parse_cues_in_order():
    doc = parse_srt(SRT_SAMPLE)
    assert doc.format == "srt"
    assert [(c.identifier, c.start_ms, c.end_ms) for c in doc.cues] == [
        ("1", 1000, 2000),
        ("2", 1500, 3000),
    ]


def test_srt_only_timestamps_change():
    out, result = normalize_doc(parse_srt(SRT_SAMPLE))
    assert out == (
        "1\n"
        "00:00:00,500 --> 00:00:01,500\n"
        "Hello\n"
        "\n"
        "2\n"
        "00:00:01,500 --> 00:00:03,000\n"
        "World\n"
    )
    assert result.total_absolute_displacement_ms == 500


def test_srt_no_overlap_is_byte_identical():
    content = (
        "1\n00:00:01,000 --> 00:00:02,000\nA\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nB\n"
    )
    out, result = normalize_doc(parse_srt(content))
    assert out == content
    assert result.total_absolute_displacement_ms == 0


def test_srt_crlf_preserved():
    content = (
        "1\r\n00:00:01,000 --> 00:00:02,000\r\nHello\r\n\r\n"
        "2\r\n00:00:01,500 --> 00:00:03,000\r\nWorld\r\n"
    )
    out, _ = normalize_doc(parse_srt(content))
    assert out == (
        "1\r\n00:00:00,500 --> 00:00:01,500\r\nHello\r\n\r\n"
        "2\r\n00:00:01,500 --> 00:00:03,000\r\nWorld\r\n"
    )
    # No bare LF may be introduced.
    assert out.count("\r\n") == len(out.splitlines())


def test_srt_multiline_text_numbering_and_settings_preserved():
    content = (
        "3\n"
        "00:00:10,000 --> 00:00:12,000\n"
        "Line one\n"
        "Line two\n"
        "\n"
        "7\n"
        "00:00:11,000 --> 00:00:13,000 X1:100 X2:200\n"
        "Other\n"
    )
    out, _ = normalize_doc(parse_srt(content))
    assert out == (
        "3\n"
        "00:00:09,000 --> 00:00:11,000\n"
        "Line one\n"
        "Line two\n"
        "\n"
        "7\n"
        "00:00:11,000 --> 00:00:13,000 X1:100 X2:200\n"
        "Other\n"
    )


def test_srt_dot_separator_and_hour_width_kept():
    content = "1\n0:00:01.000 --> 0:00:02.000\nA\n\n2\n0:00:01.500 --> 0:00:03.000\nB\n"
    out, _ = normalize_doc(parse_srt(content))
    assert "0:00:00.500 --> 0:00:01.500" in out
    assert "0:00:01.500 --> 0:00:03.000" in out


def test_srt_missing_trailing_newline_roundtrip():
    content = "1\n00:00:01,000 --> 00:00:02,000\nA"
    out, _ = normalize_doc(parse_srt(content))
    assert out == content


def test_srt_empty_document_has_no_cues():
    doc = parse_srt("")
    assert doc.cues == []
    assert render_document(doc, []) == ""


def test_srt_bom_tolerated_and_preserved():
    content = "﻿1\n00:00:01,000 --> 00:00:02,000\nA\n"
    doc = parse_srt(content)
    assert len(doc.cues) == 1
    out, _ = normalize_doc(doc)
    assert out.startswith("﻿1\n")


def test_srt_bad_index_line_reports_line_number():
    with pytest.raises(DocumentSyntaxError) as exc:
        parse_srt("oops\n00:00:01,000 --> 00:00:02,000\nA\n")
    assert exc.value.line == 1


def test_srt_bad_timing_line_reports_line_number():
    content = (
        "1\n00:00:01,000 --> 00:00:02,000\nA\n\n"
        "2\nnot a timing line\nB\n"
    )
    with pytest.raises(DocumentSyntaxError) as exc:
        parse_srt(content)
    assert exc.value.line == 6


def test_srt_missing_timing_line_reports_error():
    with pytest.raises(DocumentSyntaxError) as exc:
        parse_srt("1\n00:00:01,000 --> 00:00:02,000\nA\n\n2\n")
    assert exc.value.line == 5


def test_srt_non_positive_duration_rejected_with_line():
    with pytest.raises(DocumentSyntaxError) as exc:
        parse_srt("1\n00:00:02,000 --> 00:00:02,000\nA\n")
    assert exc.value.line == 2
    assert "duration must be positive" in exc.value.message


# --------------------------------------------------------------------------- #
# WebVTT
# --------------------------------------------------------------------------- #

WEBVTT_SAMPLE = (
    "WEBVTT - demo\n"
    "\n"
    "NOTE this is a comment\n"
    "spanning two lines\n"
    "\n"
    "STYLE\n"
    "::cue { color: lime }\n"
    "\n"
    "REGION\n"
    "id:fred\n"
    "\n"
    "cue-1\n"
    "00:00.000 --> 00:01.000 position:10%,line-left align:start\n"
    "Hello\n"
    "\n"
    "00:00.500 --> 00:02.000\n"
    "World\n"
)


def test_webvtt_parse_skips_non_cue_blocks():
    doc = parse_webvtt(WEBVTT_SAMPLE)
    assert doc.format == "webvtt"
    assert [(c.identifier, c.start_ms, c.end_ms) for c in doc.cues] == [
        ("cue-1", 0, 1000),
        (None, 500, 2000),
    ]


def test_webvtt_only_timestamps_change():
    out, result = normalize_doc(parse_webvtt(WEBVTT_SAMPLE))
    assert out == WEBVTT_SAMPLE.replace(
        "00:00.500 --> 00:02.000", "00:01.000 --> 00:02.500"
    )
    assert result.total_absolute_displacement_ms == 500
    # Untouched blocks stay byte-identical.
    for kept in (
        "WEBVTT - demo\n",
        "NOTE this is a comment\nspanning two lines\n",
        "STYLE\n::cue { color: lime }\n",
        "REGION\nid:fred\n",
        "cue-1\n00:00.000 --> 00:01.000 position:10%,line-left align:start\n",
    ):
        assert kept in out


def test_webvtt_no_overlap_is_byte_identical():
    content = "WEBVTT\n\n00:00.000 --> 00:01.000\nA\n\n00:02.000 --> 00:03.000\nB\n"
    out, _ = normalize_doc(parse_webvtt(content))
    assert out == content


def test_webvtt_long_form_timestamps_keep_hours():
    content = (
        "WEBVTT\n\n"
        "01:00:00.000 --> 01:00:02.000\nA\n\n"
        "01:00:01.000 --> 01:00:03.000\nB\n"
    )
    out, _ = normalize_doc(parse_webvtt(content))
    # The lower-median optimum pulls cue 1 earlier; hours form is kept.
    assert "00:59:59.000 --> 01:00:01.000" in out
    assert "01:00:01.000 --> 01:00:03.000" in out


def test_webvtt_crlf_preserved():
    content = "WEBVTT\r\n\r\n00:00.000 --> 00:01.000\r\nA\r\n\r\n00:00.500 --> 00:02.000\r\nB\r\n"
    out, _ = normalize_doc(parse_webvtt(content))
    assert out == content.replace("00:00.500 --> 00:02.000", "00:01.000 --> 00:02.500")
    assert out.count("\r\n") == len(out.splitlines())


def test_webvtt_bom_tolerated_and_preserved():
    content = "﻿WEBVTT\n\n00:00.000 --> 00:01.000\nA\n"
    doc = parse_webvtt(content)
    assert len(doc.cues) == 1
    out, _ = normalize_doc(doc)
    assert out.startswith("﻿WEBVTT\n")


def test_webvtt_missing_header_reports_line_1():
    with pytest.raises(DocumentSyntaxError) as exc:
        parse_webvtt("00:00.000 --> 00:01.000\nA\n")
    assert exc.value.line == 1


def test_webvtt_empty_document_rejected():
    with pytest.raises(DocumentSyntaxError) as exc:
        parse_webvtt("")
    assert exc.value.line == 1


def test_webvtt_malformed_timing_line_reports_line_number():
    content = "WEBVTT\n\nintro\n00:00.000 -> 00:01.000\nA\n"
    with pytest.raises(DocumentSyntaxError) as exc:
        parse_webvtt(content)
    assert exc.value.line == 4


def test_webvtt_block_without_timing_line_reports_line_number():
    content = "WEBVTT\n\njust some text\nmore text\n"
    with pytest.raises(DocumentSyntaxError) as exc:
        parse_webvtt(content)
    assert exc.value.line == 3


def test_webvtt_non_positive_duration_rejected_with_line():
    content = "WEBVTT\n\n00:01.000 --> 00:01.000\nA\n"
    with pytest.raises(DocumentSyntaxError) as exc:
        parse_webvtt(content)
    assert exc.value.line == 3


def test_webvtt_note_block_with_arrow_inside_is_skipped():
    content = (
        "WEBVTT\n\n"
        "NOTE 00:00.000 --> 00:01.000 is not a cue\n\n"
        "00:00.000 --> 00:01.000\nA\n"
    )
    doc = parse_webvtt(content)
    assert len(doc.cues) == 1
    out, _ = normalize_doc(doc)
    assert out == content


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def test_parse_document_dispatch():
    assert parse_document("srt", SRT_SAMPLE).format == "srt"
    assert parse_document("webvtt", WEBVTT_SAMPLE).format == "webvtt"
    with pytest.raises(ValueError):
        parse_document("ass", "")
