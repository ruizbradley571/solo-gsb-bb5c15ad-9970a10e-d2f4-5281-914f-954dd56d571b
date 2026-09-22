"""Unit tests for lossless SRT/WebVTT parsing and reconstruction."""

from __future__ import annotations

from typing import List

import pytest

from app.documents import (
    SRT,
    WEBVTT,
    DocumentSyntaxError,
    parse_document,
)


def render_unchanged(fmt: str, content: str) -> str:
    doc = parse_document(fmt, content)
    return doc.render(
        [t.start_ms for t in doc.timings],
        [t.end_ms for t in doc.timings],
    )


# --------------------------------------------------------------------------- #
# SRT
# --------------------------------------------------------------------------- #


def test_srt_basic_parse():
    content = (
        "1\n"
        "00:00:00,000 --> 00:00:01,500\n"
        "Hello\n"
        "world\n"
        "\n"
        "2\n"
        "00:00:02,000 --> 00:00:03,000\n"
        "Bye\n"
    )
    doc = parse_document(SRT, content)
    assert doc.cue_count == 2
    t0, t1 = doc.timings
    assert (t0.start_ms, t0.end_ms) == (0, 1500)
    assert (t1.start_ms, t1.end_ms) == (2000, 3000)
    assert t0.cue_id == "1"
    assert t0.text == "Hello\nworld"
    assert t1.text == "Bye"


def test_srt_numbers_and_text_never_modified():
    # Non-sequential, zero-padded numbering and multi-line text, including a
    # "-->" sequence inside cue text, must survive untouched.
    content = (
        "07\n"
        "00:00:00,000 --> 00:00:01,000\n"
        "line one -->\n"
        "line two\n"
        "\n"
        "08\n"
        "00:00:00,500 --> 00:00:02,000\n"
        "second\n"
    )
    doc = parse_document(SRT, content)
    out = doc.render([0, 1500], [1000, 3000])
    assert out == (
        "07\n"
        "00:00:00,000 --> 00:00:01,000\n"
        "line one -->\n"
        "line two\n"
        "\n"
        "08\n"
        "00:00:01,500 --> 00:00:03,000\n"
        "second\n"
    )


def test_srt_only_timing_tokens_change():
    content = "1\n  00:00:00,000  -->  00:00:01,000  \ntext\n"
    out = render_unchanged(SRT, content)
    assert out == content
    doc = parse_document(SRT, content)
    changed = doc.render([125], [1625])
    assert changed == "1\n  00:00:00,125  -->  00:00:01,625  \ntext\n"


def test_srt_crlf_preserved():
    content = (
        "1\r\n00:00:00,000 --> 00:00:01,000\r\na\r\n\r\n"
        "2\r\n00:00:00,500 --> 00:00:02,000\r\nb\r\n"
    )
    doc = parse_document(SRT, content)
    out = doc.render([0, 1500], [1000, 3000])
    assert out == (
        "1\r\n00:00:00,000 --> 00:00:01,000\r\na\r\n\r\n"
        "2\r\n00:00:01,500 --> 00:00:03,000\r\nb\r\n"
    )
    # No stray LF-only endings introduced.
    assert "\r\n" in out and "\n" not in out.replace("\r\n", "")


def test_srt_missing_final_newline_preserved():
    content = "1\n00:00:00,000 --> 00:00:01,000\nx"
    assert render_unchanged(SRT, content) == content


def test_srt_dot_separator_accepted_and_preserved():
    content = "1\n00:00:00.000 --> 00:00:01.500\nx\n"
    doc = parse_document(SRT, content)
    assert (doc.timings[0].start_ms, doc.timings[0].end_ms) == (0, 1500)
    out = doc.render([10], [1510])
    assert out == "1\n00:00:00.010 --> 00:00:01.510\nx\n"


def test_srt_hours_round_trip():
    content = "1\n01:02:03,456 --> 01:02:04,000\nx\n"
    doc = parse_document(SRT, content)
    t = doc.timings[0]
    assert t.start_ms == ((1 * 60 + 2) * 60 + 3) * 1000 + 456
    assert render_unchanged(SRT, content) == content


def test_srt_bom_tolerated():
    content = "﻿1\n00:00:00,000 --> 00:00:01,000\nx\n"
    doc = parse_document(SRT, content)
    assert doc.cue_count == 1
    assert render_unchanged(SRT, content) == content


def test_srt_empty_document():
    doc = parse_document(SRT, "")
    assert doc.cue_count == 0
    assert doc.render([], []) == ""
    doc2 = parse_document(SRT, "\n\n  \n")
    assert doc2.cue_count == 0


@pytest.mark.parametrize(
    "content,line",
    [
        # Missing timing line after number
        ("1\nhello world\n", 2),
        # Non-digit cue number
        ("abc\n00:00:00,000 --> 00:00:01,000\nx\n", 1),
        # Malformed timecode
        ("1\n00:00:00 --> 00:00:01,000\nx\n", 2),
        # Short (WebVTT-style) timestamp rejected for SRT
        ("1\n00:00.000 --> 00:01.000\nx\n", 2),
        # end <= start
        ("1\n00:00:02,000 --> 00:00:02,000\nx\n", 2),
        ("1\n00:00:03,000 --> 00:00:02,000\nx\n", 2),
        # Trailing garbage (WebVTT-style settings) on the SRT timing line
        ("1\n00:00:00,000 --> 00:00:01,000 X1\nx\n", 2),
        # Second cue malformed; line numbers count the blanks
        (
            "1\n00:00:00,000 --> 00:00:01,000\nx\n\n"
            "2\n00:00:01:000 --> 00:00:02,000\ny\n",
            6,
        ),
    ],
)
def test_srt_syntax_errors_report_line(content: str, line: int):
    with pytest.raises(DocumentSyntaxError) as exc:
        parse_document(SRT, content)
    assert exc.value.line == line


# --------------------------------------------------------------------------- #
# WebVTT
# --------------------------------------------------------------------------- #


def test_webvtt_basic_with_id_and_settings():
    content = (
        "WEBVTT\n"
        "\n"
        "cue-1\n"
        "00:00:00.000 --> 00:00:01.500 line:0 position:20%\n"
        "<b>Hi</b>\n"
        "\n"
        "00:01:00.000 --> 00:02:00.000\n"
        "no id here\n"
    )
    doc = parse_document(WEBVTT, content)
    assert doc.cue_count == 2
    t0, t1 = doc.timings
    assert (t0.start_ms, t0.end_ms) == (0, 1500)
    assert (t1.start_ms, t1.end_ms) == (60_000, 120_000)
    assert t0.cue_id == "cue-1"
    assert t0.text == "<b>Hi</b>"
    assert t1.cue_id is None
    assert render_unchanged(WEBVTT, content) == content


def test_webvtt_header_notes_styles_regions_preserved():
    content = (
        "WEBVTT - My subtitles\n"
        "\n"
        "NOTE top comment\n"
        "spanning two lines\n"
        "\n"
        "REGION\n"
        "id:top\n"
        "width:50%\n"
        "\n"
        "STYLE\n"
        "::cue { color: red }\n"
        "\n"
        "NOTE\n"
        "a bare NOTE block\n"
        "\n"
        "id-1\n"
        "00:00.000 --> 00:01.000 align:start\n"
        "first\n"
        "\n"
        "00:02.000 --> 00:03.000\n"
        "second\n"
    )
    doc = parse_document(WEBVTT, content)
    assert doc.cue_count == 2
    out = doc.render([0, 2500], [1000, 3500])
    # Everything except the two timing token pairs must remain identical.
    assert out == (
        "WEBVTT - My subtitles\n"
        "\n"
        "NOTE top comment\n"
        "spanning two lines\n"
        "\n"
        "REGION\n"
        "id:top\n"
        "width:50%\n"
        "\n"
        "STYLE\n"
        "::cue { color: red }\n"
        "\n"
        "NOTE\n"
        "a bare NOTE block\n"
        "\n"
        "id-1\n"
        "00:00.000 --> 00:01.000 align:start\n"
        "first\n"
        "\n"
        "00:02.500 --> 00:03.500\n"
        "second\n"
    )


def test_webvtt_short_and_long_timecodes():
    content = "WEBVTT\n\n00:00.500 --> 00:59.500\na\n\n01:00:00.000 --> 01:00:01.000\nb\n"
    doc = parse_document(WEBVTT, content)
    assert [t.start_ms for t in doc.timings] == [500, 3_600_000]
    assert render_unchanged(WEBVTT, content) == content


def test_webvtt_short_form_promoted_when_pushed_past_one_hour():
    content = "WEBVTT\n\n59:59.900 --> 59:59.950\nx\n"
    doc = parse_document(WEBVTT, content)
    out = doc.render([3_600_000], [3_600_050])
    assert out == "WEBVTT\n\n01:00:00.000 --> 01:00:00.050\nx\n"


def test_webvtt_crlf_preserved():
    content = (
        "WEBVTT\r\n\r\n"
        "1\r\n00:00:00.000 --> 00:00:01.000\r\na\r\n\r\n"
        "00:00:01.500 --> 00:00:02.500\r\nb\r\n"
    )
    doc = parse_document(WEBVTT, content)
    out = doc.render([0, 2000], [1000, 3000])
    assert "\r\n" in out and "\n" not in out.replace("\r\n", "")
    assert out == (
        "WEBVTT\r\n\r\n"
        "1\r\n00:00:00.000 --> 00:00:01.000\r\na\r\n\r\n"
        "00:00:02.000 --> 00:00:03.000\r\nb\r\n"
    )


def test_webvtt_bom_tolerated():
    content = "﻿WEBVTT\n\n00:00.000 --> 00:01.000\nx\n"
    doc = parse_document(WEBVTT, content)
    assert doc.cue_count == 1
    assert render_unchanged(WEBVTT, content) == content


def test_webvtt_header_only_is_empty_document():
    for content in ("WEBVTT\n", "WEBVTT - title\n\n", "WEBVTT"):
        doc = parse_document(WEBVTT, content)
        assert doc.cue_count == 0
        assert doc.render([], []) == content


@pytest.mark.parametrize(
    "content,line",
    [
        ("", 1),
        ("NOTWEBVTT\n\n00:00.000 --> 00:01.000\nx\n", 1),
        # Missing blank line after header
        ("WEBVTT\n00:00.000 --> 00:01.000\nx\n", 2),
        # Block that is neither a cue nor metadata
        ("WEBVTT\n\nmystery block\n\n00:00.000 --> 00:01.000\nx\n", 3),
        # Identifier with no following timing line
        ("WEBVTT\n\ncue-9\njust text\n", 3),
        # Comma separator (SRT style) rejected for WebVTT
        ("WEBVTT\n\n00:00:00,000 --> 00:00:01,000\nx\n", 3),
        # Bad timecode values
        ("WEBVTT\n\n00:60.000 --> 00:61.000\nx\n", 3),
        ("WEBVTT\n\n00:00.000 --> 00:00.000\nx\n", 3),
        # Error in the second cue: header(1), blank(2), cue block(3-4),
        # blank(5), bad line(6)
        ("WEBVTT\n\n00:00.000 --> 00:01.000\na\n\n??\nnot a timing\n", 6),
    ],
)
def test_webvtt_syntax_errors_report_line(content: str, line: int):
    with pytest.raises(DocumentSyntaxError) as exc:
        parse_document(WEBVTT, content)
    assert exc.value.line == line


# --------------------------------------------------------------------------- #
# Solver integration via to_cues
# --------------------------------------------------------------------------- #


def test_to_cues_document_order_and_pins():
    content = (
        "1\n00:00:00,000 --> 00:00:01,000\na\n\n"
        "2\n00:00:00,500 --> 00:00:02,000\nb\n\n"
        "3\n00:00:00,600 --> 00:00:03,000\nc\n"
    )
    doc = parse_document(SRT, content)
    cues = doc.to_cues({1})
    assert [c.index for c in cues] == [0, 1, 2]
    assert [c.pinned for c in cues] == [False, True, False]
    assert [c.cue_id for c in cues] == ["1", "2", "3"]
    assert [c.text for c in cues] == ["a", "b", "c"]


def test_render_and_reparse_is_stable():
    starts: List[int] = [10, 2050]
    ends: List[int] = [1010, 4050]
    content = "1\n00:00:00,000 --> 00:00:01,000\na\n\n2\n00:00:00,500 --> 00:00:02,000\nb\n"
    doc = parse_document(SRT, content)
    once = doc.render(starts, ends)
    again_doc = parse_document(SRT, once)
    assert [t.start_ms for t in again_doc.timings] == starts
    assert [t.end_ms for t in again_doc.timings] == ends
    assert again_doc.render(starts, ends) == once
