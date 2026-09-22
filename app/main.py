"""FastAPI application: subtitle timeline normalization service."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from .documents import DocumentSyntaxError, parse_document
from .schemas import (
    ConflictOutput,
    ConflictResponse,
    DocumentSyntaxErrorResponse,
    NormalizeDocumentRequest,
    NormalizeDocumentResponse,
    NormalizeRequest,
    NormalizeResponse,
    SegmentOutput,
)
from .solver import Cue, InfeasibleError, normalize

app = FastAPI(
    title="Subtitle Timeline Normalizer",
    version=__version__,
    description=(
        "Repairs overlapping subtitle cues while preserving text, durations "
        "and locked positions, minimizing total absolute start-time movement."
    ),
)


@app.get("/", include_in_schema=False)
def root() -> Dict[str, str]:
    return {"service": "subtitle-timeline-normalizer", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health() -> Dict[str, str]:
    """Liveness/readiness probe."""
    return {"status": "ok", "version": __version__}


@app.post(
    "/normalize",
    response_model=NormalizeResponse,
    responses={409: {"model": ConflictResponse}},
    summary="Normalize a subtitle timeline",
)
def normalize_timeline(request: NormalizeRequest) -> Any:
    cues = [
        Cue(
            index=i,
            cue_id=seg.id,
            text=seg.text,
            start_ms=seg.start_ms,
            end_ms=seg.end_ms,
            pinned=seg.pinned,
        )
        for i, seg in enumerate(request.segments)
    ]

    try:
        result = normalize(cues, min_gap_ms=request.min_gap_ms)
    except InfeasibleError as exc:
        return _conflict_response(exc)

    return NormalizeResponse(
        segments=[
            SegmentOutput(
                index=c.index,
                id=c.cue_id,
                text=c.text,
                start_ms=c.start_ms,
                end_ms=c.end_ms,
                pinned=c.pinned,
                displacement_ms=c.displacement_ms,
            )
            for c in result.cues
        ],
        min_gap_ms=request.min_gap_ms,
        total_absolute_displacement_ms=result.total_absolute_displacement_ms,
    )


def _conflict_response(exc: InfeasibleError) -> JSONResponse:
    body = ConflictResponse(
        error="infeasible_constraints",
        message=(
            "The locked segments cannot be satisfied simultaneously; "
            "see conflicts for details."
        ),
        conflicts=[ConflictOutput(**c.to_dict()) for c in exc.conflicts],
    )
    return JSONResponse(status_code=409, content=body.model_dump())


@app.post(
    "/normalize-document",
    response_model=NormalizeDocumentResponse,
    responses={
        409: {"model": ConflictResponse},
        422: {"model": DocumentSyntaxErrorResponse},
    },
    summary="Normalize a complete SRT or WebVTT document",
)
def normalize_document(request: NormalizeDocumentRequest) -> Any:
    """Repair cue timestamps while preserving the entire document verbatim.

    Only cue timing lines change: SRT numbers and (multi-line) text, and the
    WebVTT header, NOTE/STYLE/REGION blocks, cue identifiers and timing-line
    settings, as well as the original line-ending style, are all preserved.
    """
    try:
        document = parse_document(request.format, request.content)
    except DocumentSyntaxError as exc:
        body = DocumentSyntaxErrorResponse(
            error="document_syntax_error",
            format=request.format,
            line=exc.line,
            message=exc.message,
        )
        return JSONResponse(status_code=422, content=body.model_dump())

    pinned = set(request.pinned_indices or [])
    out_of_range = sorted(i for i in pinned if i >= document.cue_count)
    if out_of_range:
        body = {
            "error": "invalid_pinned_indices",
            "format": request.format,
            "message": (
                f"pinned_indices references cue index {out_of_range[0]} but the "
                f"document contains only {document.cue_count} cue(s); indices "
                "must be 0-based cue positions below that count"
            ),
            "out_of_range": out_of_range,
            "cue_count": document.cue_count,
        }
        return JSONResponse(status_code=422, content=body)

    cues = document.to_cues(pinned)

    try:
        result = normalize(cues, min_gap_ms=request.min_gap_ms)
    except InfeasibleError as exc:
        return _conflict_response(exc)

    normalized_content = document.render(
        start_ms=[c.start_ms for c in result.cues],
        end_ms=[c.end_ms for c in result.cues],
    )

    return NormalizeDocumentResponse(
        format=request.format,
        normalized_content=normalized_content,
        cue_count=document.cue_count,
        min_gap_ms=request.min_gap_ms,
        total_absolute_displacement_ms=result.total_absolute_displacement_ms,
    )
