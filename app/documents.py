"""Parsing and surgical re-rendering of SRT and WebVTT subtitle documents.

The parser locates cue timing lines and records the exact column spans of
the two timestamps; rendering rewrites only those spans. Every other byte
of the original document — SRT sequence numbers and multi-line text, the
WebVTT header, NOTE/STYLE/REGION blocks, cue identifiers, timing-line
settings, blank lines and the newline style (LF/CRLF) — is preserved
verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .solver import NormalizedCue


class DocumentSyntaxError(Exception):
    """A subtitle document that cannot be parsed.

    Carries the 1-based ``line`` number so the API can point the caller at
    the offending line.
    """

    def __init__(self, message: str, line: int):
        self.message = message
        self.line = line
        super().__init__(f"line {line}: {message}")


@dataclass(frozen=True)
class TimestampStyle:
    """How one timestamp was written, so re-rendering keeps the same shape."""

    hour_digits: Optional[int]  # None => WebVTT short form "MM:SS.mmm"
    separator: str  # "," (SRT) or "." (WebVTT)


@dataclass(frozen=True)
class DocumentCue:
    """One cue located inside the source document."""

    index: int  # 0-based position in document order
    identifier: Optional[str]  # SRT sequence number / WebVTT cue id (echo only)
    text: str  # payload text (never modified)
    start_ms: int
    end_ms: int
    line_index: int  # line of the timing line within the document
    start_span: Tuple[int, int]  # column span of the start timestamp
    end_span: Tuple[int, int]
    start_style: TimestampStyle
    end_style: TimestampStyle


@dataclass(frozen=True)
class ParsedDocument:
    format: str
    lines: List[str]  # original lines, line terminators included
    cues: List[DocumentCue]


# --------------------------------------------------------------------------- #
# Line handling
# --------------------------------------------------------------------------- #

_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)?")


def _split_lines(content: str) -> List[str]:
    """Split into lines with terminators, on CR/LF only.

    Unlike ``str.splitlines`` this ignores exotic Unicode line boundaries
    (U+2028, NEL, ...) so subtitle text is never re-wrapped, and it never
    invents a trailing empty line: ``"".join(_split_lines(c)) == c``.
    """
    lines: List[str] = []
    pos = 0
    while pos < len(content):
        m = _LINE_RE.match(content, pos)
        assert m is not None and m.end() > pos, "line splitter made no progress"
        lines.append(m.group(0))
        pos = m.end()
    return lines


def _split_eol(line: str) -> Tuple[str, str]:
    """Split a line into (body, terminator)."""
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\r", "\n")):
        return line[:-1], line[-1]
    return line, ""


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #


def _srt_ts(prefix: str) -> str:
    # SRT: "H:MM:SS,mmm" (hours of any width; "," canonical, "." tolerated).
    return (
        rf"(?P<{prefix}_h>\d+):(?P<{prefix}_m>\d{{2}}):(?P<{prefix}_s>\d{{2}})"
        rf"(?P<{prefix}_sep>[,.])(?P<{prefix}_ms>\d{{3}})"
    )


def _vtt_ts(prefix: str) -> str:
    # WebVTT: "MM:SS.mmm" or "HH...H:MM:SS.mmm" (always a dot).
    return (
        rf"(?:(?P<{prefix}_h>\d+):)?(?P<{prefix}_m>\d{{2}}):(?P<{prefix}_s>\d{{2}})"
        rf"\.(?P<{prefix}_ms>\d{{3}})"
    )


_SRT_TIMING_RE = re.compile(
    r"[ \t]*" + _srt_ts("a") + r"[ \t]*-->[ \t]*" + _srt_ts("b") + r"[^\r\n]*"
)

_VTT_TIMING_RE = re.compile(
    r"[ \t]*"
    + _vtt_ts("a")
    + r"[ \t]+-->[ \t]+"
    + _vtt_ts("b")
    + r"(?:[ \t]+[^\r\n]*)?$"
)

_SRT_INDEX_RE = re.compile(r"[ \t]*\d+[ \t]*")


def _read_timestamp(
    match: "re.Match[str]", prefix: str, default_separator: str
) -> Tuple[int, TimestampStyle, Tuple[int, int]]:
    """Extract (milliseconds, style, column span) for one matched timestamp."""
    hours = match.group(f"{prefix}_h")
    sep_group = f"{prefix}_sep"
    separator = default_separator
    if sep_group in match.re.groupindex:
        separator = match.group(sep_group)
    total_ms = (
        ((int(hours) if hours else 0) * 60 + int(match.group(f"{prefix}_m"))) * 60
        + int(match.group(f"{prefix}_s"))
    ) * 1000 + int(match.group(f"{prefix}_ms"))
    first_group = f"{prefix}_h" if hours is not None else f"{prefix}_m"
    span = (match.start(first_group), match.end(f"{prefix}_ms"))
    style = TimestampStyle(
        hour_digits=len(hours) if hours is not None else None,
        separator=separator,
    )
    return total_ms, style, span


def format_timestamp(ms: int, style: TimestampStyle) -> str:
    """Render ``ms`` in the same shape the source timestamp used."""
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    if style.hour_digits is None:
        # WebVTT short form: the minutes field absorbs the hours.
        total_minutes = hours * 60 + minutes
        return f"{total_minutes:02d}:{seconds:02d}{style.separator}{millis:03d}"
    return (
        f"{hours:0{style.hour_digits}d}:{minutes:02d}:{seconds:02d}"
        f"{style.separator}{millis:03d}"
    )


# --------------------------------------------------------------------------- #
# SRT
# --------------------------------------------------------------------------- #


def parse_srt(content: str) -> ParsedDocument:
    """Parse an SRT document, locating every cue timing line."""
    lines = _split_lines(content)
    cues: List[DocumentCue] = []
    i = 0
    n = len(lines)
    while i < n:
        body, _ = _split_eol(lines[i])
        check = body.lstrip("\ufeff") if i == 0 else body  # tolerate a BOM
        if check.strip() == "":
            i += 1
            continue
        if _SRT_INDEX_RE.fullmatch(check) is None:
            raise DocumentSyntaxError(
                "expected a cue sequence number (a non-negative integer)",
                line=i + 1,
            )
        identifier = check.strip()
        timing_line = i + 1
        if timing_line >= n:
            raise DocumentSyntaxError(
                f"cue {identifier} is missing its timing line "
                "('HH:MM:SS,mmm --> HH:MM:SS,mmm')",
                line=i + 1,
            )
        timing_body, _ = _split_eol(lines[timing_line])
        m = _SRT_TIMING_RE.match(timing_body)
        if m is None:
            raise DocumentSyntaxError(
                "malformed timing line: expected "
                "'HH:MM:SS,mmm --> HH:MM:SS,mmm'",
                line=timing_line + 1,
            )
        start_ms, start_style, start_span = _read_timestamp(m, "a", ",")
        end_ms, end_style, end_span = _read_timestamp(m, "b", ",")
        if end_ms <= start_ms:
            raise DocumentSyntaxError(
                f"cue {identifier} ends at or before it starts "
                f"({start_ms} ms -> {end_ms} ms); duration must be positive",
                line=timing_line + 1,
            )
        # Payload: everything up to the next blank line (may be multi-line).
        j = timing_line + 1
        text_lines: List[str] = []
        while j < n:
            payload_body, _ = _split_eol(lines[j])
            if payload_body.strip() == "":
                break
            text_lines.append(payload_body)
            j += 1
        cues.append(
            DocumentCue(
                index=len(cues),
                identifier=identifier,
                text="\n".join(text_lines),
                start_ms=start_ms,
                end_ms=end_ms,
                line_index=timing_line,
                start_span=start_span,
                end_span=end_span,
                start_style=start_style,
                end_style=end_style,
            )
        )
        i = j
    return ParsedDocument(format="srt", lines=lines, cues=cues)


# --------------------------------------------------------------------------- #
# WebVTT
# --------------------------------------------------------------------------- #


def parse_webvtt(content: str) -> ParsedDocument:
    """Parse a WebVTT document, locating every cue timing line."""
    lines = _split_lines(content)
    if not lines:
        raise DocumentSyntaxError(
            "empty document: a WebVTT file must start with the 'WEBVTT' header",
            line=1,
        )
    first_body, _ = _split_eol(lines[0])
    header = first_body.lstrip("\ufeff")  # tolerate a BOM
    if header != "WEBVTT" and not header.startswith(("WEBVTT ", "WEBVTT\t")):
        raise DocumentSyntaxError(
            "expected the 'WEBVTT' header on the first line",
            line=1,
        )

    cues: List[DocumentCue] = []
    n = len(lines)
    i = 1
    # Header metadata runs until the first blank line.
    while i < n and _split_eol(lines[i])[0].strip() != "":
        i += 1

    while i < n:
        body, _ = _split_eol(lines[i])
        if body.strip() == "":
            i += 1
            continue
        # Gather one block (up to the next blank line).
        j = i
        while j < n and _split_eol(lines[j])[0].strip() != "":
            j += 1
        block = [_split_eol(lines[k])[0] for k in range(i, j)]
        head = block[0]
        if (
            head == "STYLE"
            or head == "REGION"
            or head == "NOTE"
            or head.startswith(("NOTE ", "NOTE\t"))
        ):
            i = j
            continue

        # A cue: optional identifier line, then the timing line.
        if "-->" in head:
            identifier = None
            timing_offset = 0
        elif len(block) > 1 and "-->" in block[1]:
            identifier = head
            timing_offset = 1
        else:
            # No well-formed timing line: point at the line that was meant
            # to be one (a broken "->" arrow marks it), else at the block.
            bad = 0
            for k, text in enumerate(block):
                if "->" in text:
                    bad = k
                    break
            raise DocumentSyntaxError(
                "expected a cue timing line ('MM:SS.mmm --> MM:SS.mmm') "
                "or a NOTE/STYLE/REGION block",
                line=i + bad + 1,
            )
        timing_body = block[timing_offset]
        m = _VTT_TIMING_RE.match(timing_body)
        if m is None:
            raise DocumentSyntaxError(
                "malformed cue timing line: expected "
                "'MM:SS.mmm --> MM:SS.mmm' with optional settings",
                line=i + timing_offset + 1,
            )
        start_ms, start_style, start_span = _read_timestamp(m, "a", ".")
        end_ms, end_style, end_span = _read_timestamp(m, "b", ".")
        if end_ms <= start_ms:
            raise DocumentSyntaxError(
                f"cue ends at or before it starts ({start_ms} ms -> {end_ms} ms); "
                "duration must be positive",
                line=i + timing_offset + 1,
            )
        payload = block[timing_offset + 1 :]
        cues.append(
            DocumentCue(
                index=len(cues),
                identifier=identifier,
                text="\n".join(payload),
                start_ms=start_ms,
                end_ms=end_ms,
                line_index=i + timing_offset,
                start_span=start_span,
                end_span=end_span,
                start_style=start_style,
                end_style=end_style,
            )
        )
        i = j
    return ParsedDocument(format="webvtt", lines=lines, cues=cues)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def parse_document(fmt: str, content: str) -> ParsedDocument:
    """Parse ``content`` as ``fmt`` (``"srt"`` or ``"webvtt"``)."""
    if fmt == "srt":
        return parse_srt(content)
    if fmt == "webvtt":
        return parse_webvtt(content)
    raise ValueError(f"unsupported document format: {fmt!r}")


def render_document(doc: ParsedDocument, cues: List[NormalizedCue]) -> str:
    """Rewrite only the cue timestamps; everything else stays byte-identical."""
    lines = list(doc.lines)
    for dc, nc in zip(doc.cues, cues):
        assert dc.index == nc.index, "solver results must stay in document order"
        body, eol = _split_eol(lines[dc.line_index])
        new_start = format_timestamp(nc.start_ms, dc.start_style)
        new_end = format_timestamp(nc.end_ms, dc.end_style)
        (s0, s1), (e0, e1) = dc.start_span, dc.end_span
        body = body[:s0] + new_start + body[s1:e0] + new_end + body[e1:]
        lines[dc.line_index] = body + eol
    return "".join(lines)
