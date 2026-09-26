"""FastAPI application: subtitle timeline normalization service."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from .documents import DocumentSyntaxError, parse_document, render_document
from .schemas import (
    ConflictOutput,
    ConflictResponse,
    DocumentErrorResponse,
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


def _conflict_response(exc: InfeasibleError) -> JSONResponse:
    """Shared 409 body for both normalization endpoints."""
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


@app.post(
    "/normalize-document",
    response_model=NormalizeDocumentResponse,
    responses={
        409: {"model": ConflictResponse},
        422: {"model": DocumentErrorResponse},
    },
    summary="Normalize an SRT or WebVTT subtitle document",
)
def normalize_document(request: NormalizeDocumentRequest) -> Any:
    try:
        doc = parse_document(request.format.value, request.content)
    except DocumentSyntaxError as exc:
        body = DocumentErrorResponse(
            error="document_syntax_error",
            message=exc.message,
            line=exc.line,
        )
        return JSONResponse(status_code=422, content=body.model_dump())

    cue_count = len(doc.cues)
    pinned = set(request.pinned_indices)
    out_of_range = sorted(idx for idx in pinned if idx >= cue_count)
    if out_of_range:
        detail = (
            f"valid indices are 0..{cue_count - 1}"
            if cue_count
            else "the document contains no cues"
        )
        body = DocumentErrorResponse(
            error="invalid_pinned_indices",
            message=(
                f"pinned_indices {out_of_range} out of range: the document "
                f"contains {cue_count} cue(s); {detail}"
            ),
            line=None,
        )
        return JSONResponse(status_code=422, content=body.model_dump())

    cues = [
        Cue(
            index=dc.index,
            cue_id=dc.identifier,
            text=dc.text,
            start_ms=dc.start_ms,
            end_ms=dc.end_ms,
            pinned=dc.index in pinned,
        )
        for dc in doc.cues
    ]

    try:
        result = normalize(cues, min_gap_ms=request.min_gap_ms)
    except InfeasibleError as exc:
        return _conflict_response(exc)

    return NormalizeDocumentResponse(
        format=request.format,
        normalized_content=render_document(doc, result.cues),
        min_gap_ms=request.min_gap_ms,
        cue_count=cue_count,
        total_absolute_displacement_ms=result.total_absolute_displacement_ms,
    )
