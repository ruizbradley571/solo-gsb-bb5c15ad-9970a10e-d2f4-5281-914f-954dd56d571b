"""Pydantic request/response models for the normalization API."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SegmentInput(BaseModel):
    """One subtitle segment in fixed playback order.

    Extra fields are ignored so callers can send full subtitle records
    (e.g. style or speaker tags) without errors.
    """

    model_config = ConfigDict(extra="ignore")

    id: Optional[str] = Field(
        default=None,
        description="Optional caller-assigned identifier, echoed in the response.",
    )
    text: str = Field(..., description="Subtitle text. Never modified by the API.")
    start_ms: int = Field(..., ge=0, description="Start time in milliseconds.")
    end_ms: int = Field(..., ge=0, description="End time in milliseconds (exclusive).")
    pinned: bool = Field(
        default=False,
        description="When true, the segment is immovable (locked to start_ms).",
    )

    @model_validator(mode="after")
    def _validate_window(self) -> "SegmentInput":
        if self.end_ms <= self.start_ms:
            raise ValueError(
                f"end_ms ({self.end_ms}) must be greater than start_ms "
                f"({self.start_ms}); segment duration must be positive"
            )
        return self


class NormalizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    segments: List[SegmentInput] = Field(
        default_factory=list,
        description="Segments in fixed playback order; order is never changed.",
    )
    min_gap_ms: int = Field(
        default=0,
        ge=0,
        description="Required gap in milliseconds between consecutive segments.",
    )


class SegmentOutput(BaseModel):
    index: int = Field(..., description="0-based position of the segment in the input.")
    id: Optional[str] = None
    text: str
    start_ms: int = Field(..., description="Repaired start time in milliseconds.")
    end_ms: int = Field(..., description="Repaired end time in milliseconds.")
    pinned: bool
    displacement_ms: int = Field(
        ..., description="|new start - original start| for this segment."
    )


class NormalizeResponse(BaseModel):
    segments: List[SegmentOutput]
    min_gap_ms: int
    total_absolute_displacement_ms: int = Field(
        ...,
        description="Sum of displacement_ms over all segments; the minimized value.",
    )


class ConflictOutput(BaseModel):
    from_index: Optional[int] = Field(
        ..., description="0-based index of the earlier locked segment; null = timeline start (0 ms)."
    )
    from_id: Optional[str] = None
    to_index: int
    to_id: Optional[str] = None
    reason_code: str
    reason: str = Field(..., description="Human-readable explanation of the conflict.")
    required_start_ms: int
    actual_start_ms: int
    shortfall_ms: int


class ConflictResponse(BaseModel):
    """Body returned with HTTP 409 when the constraints have no solution."""

    error: str = Field(..., examples=["infeasible_constraints"])
    message: str
    conflicts: List[ConflictOutput]


# --------------------------------------------------------------------------- #
# Whole-document endpoints (SRT / WebVTT)
# --------------------------------------------------------------------------- #


DocumentFormat = Literal["srt", "webvtt"]


class NormalizeDocumentRequest(BaseModel):
    """A complete SRT or WebVTT document to normalize in place."""

    model_config = ConfigDict(extra="ignore")

    format: DocumentFormat = Field(
        ..., description="Document format: 'srt' or 'webvtt'."
    )
    content: str = Field(
        ...,
        description=(
            "Raw document text. Only cue timestamps are modified; the header, "
            "comments/styles/regions, cue identifiers, settings, text and line "
            "endings are preserved."
        ),
    )
    min_gap_ms: int = Field(
        ...,
        ge=0,
        description="Required gap in milliseconds between consecutive cues.",
    )
    pinned_indices: Optional[List[int]] = Field(
        default=None,
        description=(
            "0-based positions of immovable cues in document order "
            "(headers and NOTE/STYLE/REGION blocks are not counted). "
            "Defaults to no locked cues."
        ),
    )

    @model_validator(mode="after")
    def _validate_pinned_indices(self) -> "NormalizeDocumentRequest":
        if self.pinned_indices is None:
            return self
        seen: set[int] = set()
        for value in self.pinned_indices:
            if value < 0:
                raise ValueError(
                    f"pinned_indices entries must be >= 0 (got {value})"
                )
            if value in seen:
                raise ValueError(f"pinned_indices contains a duplicate: {value}")
            seen.add(value)
        return self


class NormalizeDocumentResponse(BaseModel):
    format: DocumentFormat
    normalized_content: str = Field(
        ...,
        description=(
            "The document with repaired cue timestamps; identical to the input "
            "apart from the two timestamps on each cue timing line."
        ),
    )
    cue_count: int = Field(..., description="Number of cues found in the document.")
    min_gap_ms: int
    total_absolute_displacement_ms: int = Field(
        ...,
        description="Sum of |new start - original start| over all cues.",
    )


class DocumentSyntaxErrorResponse(BaseModel):
    """Body returned with HTTP 422 for malformed SRT/WebVTT content."""

    error: str = Field(..., examples=["document_syntax_error"])
    format: DocumentFormat
    line: int = Field(..., description="1-based line number of the syntax error.")
    message: str
