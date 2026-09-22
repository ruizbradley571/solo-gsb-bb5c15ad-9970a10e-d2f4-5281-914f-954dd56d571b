"""SRT and WebVTT document parsing and lossless reconstruction.

Only cue timing lines are ever modified. Everything else -- SRT sequence
numbers and multi-line cue text, the WebVTT ``WEBVTT`` header,
``NOTE``/``STYLE``/``REGION`` blocks, cue identifiers and the settings
trailing WebVTT timing lines -- is kept byte-for-byte in its original
position, including the original line-ending style (``\\n`` vs. ``\\r\\n``).

Parsers extract, per cue in document order:

* 0-based cue index (header/comment/style blocks do not count),
* optional identifier (the raw SRT number line or WebVTT id line),
* start/end in milliseconds,
* the character spans of the two timestamps inside the *raw* timing line,
  together with the formatting style each timestamp was written in,
* the cue text lines (passed to the solver, which never modifies them).

Reconstruction splices freshly rendered timestamps back into the original
timing-line substrings, so untouched surrounding content survives verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .solver import Cue

SRT = "srt"
WEBVTT = "webvtt"
FORMATS = (SRT, WEBVTT)


class DocumentSyntaxError(Exception):
    """A document violates the format grammar.

    ``line`` is 1-based and points at the offending line in the original
    document (counting every line, including headers and blank lines).
    """

    def __init__(self, line: int, message: str):
        self.line = line
        self.message = message
        super().__init__(f"line {line}: {message}")


# --------------------------------------------------------------------------- #
# Timecodes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _TimecodeStyle:
    """How one timestamp was written, so it can be rendered back faithfully.

    ``hours_digits`` is ``None`` for WebVTT short form ``MM:SS.mmm``;
    otherwise it is the width of the hours field (long ``HH:MM:SS.mmm``).
    """

    hours_digits: Optional[int]
    separator: str


_TC_RE = re.compile(
    r"(?:(?P<h>\d+):)?(?P<m>\d{2}):(?P<s>\d{2})(?P<sep>[.,])(?P<f>\d{1,3})"
)


def _parse_timecode(token: str) -> Tuple[int, _TimecodeStyle]:
    match = _TC_RE.fullmatch(token)
    if match is None:
        raise ValueError(f"invalid timecode {token!r}")
    hours_token = match.group("h")
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    fraction = match.group("f")
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"invalid timecode {token!r}: minutes/seconds must be < 60")
    hours = int(hours_token) if hours_token is not None else 0
    millis = ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(
        fraction.ljust(3, "0")
    )
    style = _TimecodeStyle(
        hours_digits=(len(hours_token) if hours_token is not None else None),
        separator=match.group("sep"),
    )
    return millis, style


def _render_timecode(value_ms: int, style: _TimecodeStyle) -> str:
    hours, rem = divmod(value_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    if style.hours_digits is None:
        # WebVTT short form cannot express hours; promote to long form if
        # normalization pushed the cue to/over the one-hour boundary.
        if hours == 0:
            return f"{minutes:02d}:{seconds:02d}{style.separator}{millis:03d}"
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}{style.separator}{millis:03d}"
    width = max(2, style.hours_digits)
    return (
        f"{hours:0{width}d}:{minutes:02d}:{seconds:02d}"
        f"{style.separator}{millis:03d}"
    )


# --------------------------------------------------------------------------- #
# Parsed representation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Timing:
    """One cue timing line located inside the raw document."""

    cue_index: int
    line_index: int
    cue_id: Optional[str]
    start_ms: int
    end_ms: int
    start_span: Tuple[int, int]
    end_span: Tuple[int, int]
    start_style: _TimecodeStyle
    end_style: _TimecodeStyle
    text: str


@dataclass(frozen=True)
class ParsedDocument:
    fmt: str
    # Raw lines *with* their original terminators ("" only for a missing
    # final terminator); join reproduces the input up to timestamp edits.
    lines: List[str] = field(default_factory=list)
    timings: List[_Timing] = field(default_factory=list)

    @property
    def cue_count(self) -> int:
        return len(self.timings)

    def to_cues(self, pinned_indices: set[int]) -> List[Cue]:
        return [
            Cue(
                index=t.cue_index,
                cue_id=t.cue_id,
                text=t.text,
                start_ms=t.start_ms,
                end_ms=t.end_ms,
                pinned=(t.cue_index in pinned_indices),
            )
            for t in self.timings
        ]

    def render(
        self, start_ms: List[int], end_ms: List[int]
    ) -> str:
        """Splice new timestamps into copies of the raw timing lines."""
        lines = list(self.lines)
        for timing, new_start, new_end in zip(self.timings, start_ms, end_ms):
            raw = lines[timing.line_index]
            s0, s1 = timing.start_span
            e0, e1 = timing.end_span
            new_token = _render_timecode(new_start, timing.start_style)
            end_token = _render_timecode(new_end, timing.end_style)
            # Spans are ordered left-to-right and never overlap; replace the
            # right span first so the left offsets stay valid.
            raw = raw[:e0] + end_token + raw[e1:]
            raw = raw[:s0] + new_token + raw[s1:]
            lines[timing.line_index] = raw
        return "".join(lines)


# --------------------------------------------------------------------------- #
# Line splitting (terminators preserved)
# --------------------------------------------------------------------------- #


def _split_lines(content: str) -> List[str]:
    r"""Split into lines keeping every terminator.

    ``"a\nb\r\nc"`` -> ``["a\n", "b\r\n", "c"]``; ``""`` -> ``[]``.
    Both ``\\r\\n`` and lone ``\\n``/``\\r`` are recognized, but lines are
    never re-joined differently: reconstruction simply concatenates them.
    """
    return re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", content)[:-1] if content else []


def _is_blank(line: str) -> bool:
    return line.strip() == ""


# --------------------------------------------------------------------------- #
# Timing-line grammars
# --------------------------------------------------------------------------- #

# SRT: long form only (HH:MM:SS,fraction); comma is canonical but the dot
# separator (emitted by some tools) is accepted and preserved.
_SRT_CORE = r"\d+:\d{2}:\d{2}[.,]\d{1,3}"
_SRT_TIMING_RE = re.compile(
    rf"^[ \t]*(?P<start>{_SRT_CORE})[ \t]+-->[ \t]+(?P<end>{_SRT_CORE})[ \t]*$"
)

# WebVTT: long HH:MM:SS.fraction or short MM:SS.fraction, dot separator;
# optional whitespace and cue settings follow the end timecode.
_VTT_CORE = r"(?:\d+:\d{2}:\d{2}\.\d{1,3}|\d{2}:\d{2}\.\d{1,3})"
_VTT_TIMING_RE = re.compile(
    rf"^[ \t]*(?P<start>{_VTT_CORE})[ \t]+-->[ \t]+"
    rf"(?P<end>{_VTT_CORE})(?P<rest>[ \t].*)?$"
)


def _has_arrow(line: str) -> bool:
    return "-->" in line


def _parse_timing_line(
    line: str, fmt: str, line_number: int
) -> Tuple[int, int, Tuple[int, int], Tuple[int, int], _TimecodeStyle, _TimecodeStyle]:
    pattern = _SRT_TIMING_RE if fmt == SRT else _VTT_TIMING_RE
    # Match against the content without the terminator; a leading BOM (only
    # possible on the document's first line) is tolerated too. The BOM sits
    # before the match, so it shifts spans; the terminator sits after the
    # match and does not.
    core = line.rstrip("\r\n")
    prefix_len = 1 if core.startswith("\ufeff") else 0
    match = pattern.match(core[prefix_len:])
    if match is None:
        if _has_arrow(line):
            raise DocumentSyntaxError(
                line_number,
                f"malformed {fmt.upper()} timing line: expected "
                "'start --> end'"
                + ("" if fmt == SRT else " with optional settings"),
            )
        raise DocumentSyntaxError(
            line_number, f"expected a {fmt.upper()} timing line ('-->')"
        )
    try:
        start_ms, start_style = _parse_timecode(match.group("start"))
        end_ms, end_style = _parse_timecode(match.group("end"))
    except ValueError:
        raise DocumentSyntaxError(
            line_number, f"invalid timecode in {fmt.upper()} timing line"
        )
    if end_ms <= start_ms:
        raise DocumentSyntaxError(
            line_number,
            f"cue end time ({end_ms} ms) must be greater than start time "
            f"({start_ms} ms)",
        )
    start_span = tuple(v + prefix_len for v in match.span("start"))
    end_span = tuple(v + prefix_len for v in match.span("end"))
    return (
        start_ms,
        end_ms,
        start_span,  # type: ignore[return-value]
        end_span,  # type: ignore[return-value]
        start_style,
        end_style,
    )


def _nonempty_blocks(lines: List[str], start: int = 0) -> List[Tuple[int, int]]:
    """Ranges [a, b) of consecutive non-blank lines, blank lines excluded."""
    blocks: List[Tuple[int, int]] = []
    i = start
    n = len(lines)
    while i < n:
        if _is_blank(lines[i]):
            i += 1
            continue
        a = i
        while i < n and not _is_blank(lines[i]):
            i += 1
        blocks.append((a, i))
    return blocks


# --------------------------------------------------------------------------- #
# SRT
# --------------------------------------------------------------------------- #

_DIGITS_RE = re.compile(r"\d+$")


def _parse_srt(content: str) -> ParsedDocument:
    lines = _split_lines(content)
    timings: List[_Timing] = []

    for block_a, block_b in _nonempty_blocks(lines):
        first = lines[block_a]
        if _has_arrow(first):
            # Number-less SRT cue: the block opens directly with the timing
            # line (a form emitted by several tools).
            timing_idx = block_a
            cue_id: Optional[str] = None
        else:
            # Canonical form: a digits-only number line followed immediately
            # by the timing line. Lines of cue text may themselves contain
            # "-->", so the timing line is located positionally, not by
            # scanning the whole block.
            candidate = first.strip().lstrip("\ufeff")
            if _DIGITS_RE.fullmatch(candidate) is None:
                raise DocumentSyntaxError(
                    block_a + 1,
                    "SRT cue must start with its number followed by the "
                    "timing line",
                )
            if block_a + 1 >= block_b or not _has_arrow(lines[block_a + 1]):
                raise DocumentSyntaxError(
                    block_a + 2,
                    "expected the SRT timing line ('-->') after the cue number",
                )
            cue_id = candidate
            timing_idx = block_a + 1

        (
            start_ms,
            end_ms,
            start_span,
            end_span,
            start_style,
            end_style,
        ) = _parse_timing_line(lines[timing_idx], SRT, timing_idx + 1)

        text = "\n".join(
            lines[i].rstrip("\r\n") for i in range(timing_idx + 1, block_b)
        )
        timings.append(
            _Timing(
                cue_index=len(timings),
                line_index=timing_idx,
                cue_id=cue_id,
                start_ms=start_ms,
                end_ms=end_ms,
                start_span=start_span,
                end_span=end_span,
                start_style=start_style,
                end_style=end_style,
                text=text,
            )
        )

    return ParsedDocument(fmt=SRT, lines=lines, timings=timings)


# --------------------------------------------------------------------------- #
# WebVTT
# --------------------------------------------------------------------------- #

_HEADER_RE = re.compile(r"^WEBVTT(?:[ \t].*)?$")


def _is_metadata_block(first_line: str) -> Optional[str]:
    """Return ``NOTE``/``STYLE``/``REGION`` when a block is metadata."""
    stripped = first_line.strip()
    if stripped == "STYLE":
        return "STYLE"
    if stripped == "REGION":
        return "REGION"
    if stripped == "NOTE" or stripped.startswith("NOTE ") or stripped.startswith("NOTE\t"):
        return "NOTE"
    return None


def _parse_webvtt(content: str) -> ParsedDocument:
    lines = _split_lines(content)
    if not lines:
        raise DocumentSyntaxError(1, "missing 'WEBVTT' header")
    header = lines[0].strip().lstrip("\ufeff")
    if _HEADER_RE.match(header) is None:
        raise DocumentSyntaxError(
            1, "WebVTT document must start with a 'WEBVTT' header line"
        )
    if len(lines) >= 2 and not _is_blank(lines[1]):
        raise DocumentSyntaxError(
            2, "expected a blank line after the 'WEBVTT' header"
        )

    timings: List[_Timing] = []
    for block_a, block_b in _nonempty_blocks(lines, start=1):
        first = lines[block_a]
        if _is_metadata_block(first) is not None:
            # NOTE/STYLE/REGION blocks are preserved verbatim and never
            # contribute cues.
            continue

        if _has_arrow(first):
            timing_idx = block_a
            cue_id: Optional[str] = None
        else:
            # The first line is a cue identifier; the timing line must
            # follow immediately.
            cue_id = first.rstrip("\r\n")
            if block_a + 1 >= block_b or not _has_arrow(lines[block_a + 1]):
                raise DocumentSyntaxError(
                    block_a + 1,
                    "WebVTT cue is missing a timing line ('-->') after its "
                    "identifier",
                )
            timing_idx = block_a + 1

        (
            start_ms,
            end_ms,
            start_span,
            end_span,
            start_style,
            end_style,
        ) = _parse_timing_line(lines[timing_idx], WEBVTT, timing_idx + 1)

        # Cue text lines follow verbatim; '-->' is legal there, only the
        # identifier line forbids it.
        text = "\n".join(
            lines[i].rstrip("\r\n") for i in range(timing_idx + 1, block_b)
        )
        timings.append(
            _Timing(
                cue_index=len(timings),
                line_index=timing_idx,
                cue_id=cue_id,
                start_ms=start_ms,
                end_ms=end_ms,
                start_span=start_span,
                end_span=end_span,
                start_style=start_style,
                end_style=end_style,
                text=text,
            )
        )

    return ParsedDocument(fmt=WEBVTT, lines=lines, timings=timings)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def parse_document(fmt: str, content: str) -> ParsedDocument:
    """Parse an SRT or WebVTT document.

    Raises :class:`DocumentSyntaxError` (with a 1-based line number) on any
    grammar violation.
    """
    if fmt == SRT:
        return _parse_srt(content)
    if fmt == WEBVTT:
        return _parse_webvtt(content)
    raise ValueError(f"unsupported document format: {fmt!r}")
