"""FastAPI application: subtitle timeline normalization service."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from .schemas import (
    ConflictOutput,
    ConflictResponse,
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
        body = ConflictResponse(
            error="infeasible_constraints",
            message=(
                "The locked segments cannot be satisfied simultaneously; "
                "see conflicts for details."
            ),
            conflicts=[ConflictOutput(**c.to_dict()) for c in exc.conflicts],
        )
        return JSONResponse(status_code=409, content=body.model_dump())

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
