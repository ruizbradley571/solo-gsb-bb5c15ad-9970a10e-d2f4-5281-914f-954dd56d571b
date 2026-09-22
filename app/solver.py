"""Core subtitle timeline normalization solver.

Problem statement
-----------------
Given cues ``i = 0 .. n-1`` in fixed order, each with an integer
millisecond window ``[a_i, b_i)`` of fixed duration ``d_i = b_i - a_i``,
a global minimum gap ``g`` between consecutive cues, and a set of
*immovable* (pinned/locked) cues, find new integer start times ``x_i``
that

1. preserve every cue's text and duration (``new_end_i - new_start_i == d_i``),
2. preserve every pinned cue's position (``x_i == a_i``),
3. never overlap and keep the minimum gap
   (``x_{i+1} >= x_i + d_i + g``) and never start before 0 (``x_0 >= 0``),
4. minimize the total absolute displacement
   ``sum_i |x_i - a_i|``.

Reduction to L1 isotonic regression
------------------------------------
Define cumulative spacing constants::

    c_0 = 0
    c_i = sum_{j < i} (d_j + g)

and shifted variables ``y_i = x_i - c_i`` with observations
``t_i = a_i - c_i``. Then all ordering constraints collapse to a single
monotonicity requirement::

    y_0 >= 0,  y_{i+1} >= y_i

and the objective becomes ``sum_i |y_i - t_i|`` (pinned cues carry an
infinite weight and are fixed observations). The ``y_0 >= 0`` boundary is
modeled as a virtual pinned observation ``(index -1, value 0)``.

The solution is L1 isotonic regression with pinned observations, solved
with the Pool Adjacent Violators Algorithm (PAVA) using block weighted
medians. All arithmetic is integer, so results are exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


class InfeasibleError(Exception):
    """Raised when the constraints admit no timeline."""

    def __init__(self, conflicts: List["Conflict"]):
        self.conflicts = conflicts
        super().__init__("; ".join(c.reason for c in conflicts))


@dataclass(frozen=True)
class Cue:
    """Input cue in playback order."""

    index: int
    cue_id: Optional[str]
    text: str
    start_ms: int
    end_ms: int
    pinned: bool

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class Conflict:
    """A single feasibility violation between two locked positions."""

    from_index: Optional[int]  # None means the timeline-start boundary (0 ms)
    from_id: Optional[str]
    to_index: int
    to_id: Optional[str]
    reason_code: str
    reason: str
    required_start_ms: int
    actual_start_ms: int
    shortfall_ms: int

    def to_dict(self) -> dict:
        return {
            "from_index": self.from_index,
            "from_id": self.from_id,
            "to_index": self.to_index,
            "to_id": self.to_id,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "required_start_ms": self.required_start_ms,
            "actual_start_ms": self.actual_start_ms,
            "shortfall_ms": self.shortfall_ms,
        }


@dataclass(frozen=True)
class NormalizedCue:
    index: int
    cue_id: Optional[str]
    text: str
    start_ms: int
    end_ms: int
    pinned: bool
    displacement_ms: int


@dataclass(frozen=True)
class NormalizationResult:
    cues: List[NormalizedCue]
    total_absolute_displacement_ms: int


# --------------------------------------------------------------------------- #
# PAVA blocks
# --------------------------------------------------------------------------- #


@dataclass
class _Block:
    """A PAVA block: a pool of observations sharing one fitted value.

    ``values`` holds the sorted shifted observations of the free cues.
    A block containing pinned observations keeps their common value in
    ``pin`` (the feasibility check guarantees that observations pooled
    together never demand different pin values).
    """

    values: List[int]
    pin: Optional[int]
    members: List[int] = field(default_factory=list)

    @property
    def fit(self) -> int:
        if self.pin is not None:
            return self.pin
        return _lower_weighted_median(self.values)


def _lower_weighted_median(sorted_values: List[int]) -> int:
    """Smallest value v with cumulative (unit) weight >= half of total.

    The lower median is always an L1 minimizer; choosing the lowest one
    makes the output deterministic and pushes cues as early as possible
    among equally optimal solutions.
    """
    total = len(sorted_values)
    need = (total + 1) // 2  # smallest k with 2k >= total
    return sorted_values[need - 1]


def _merge_blocks(left: _Block, right: _Block) -> _Block:
    # Merge the two sorted observation lists (both are already sorted).
    merged: List[int] = []
    i = j = 0
    while i < len(left.values) and j < len(right.values):
        if left.values[i] <= right.values[j]:
            merged.append(left.values[i])
            i += 1
        else:
            merged.append(right.values[j])
            j += 1
    merged.extend(left.values[i:])
    merged.extend(right.values[j:])

    pin: Optional[int]
    if left.pin is None:
        pin = right.pin
    elif right.pin is None:
        pin = left.pin
    else:
        # Feasibility check guarantees equal pin values can only pool here.
        assert left.pin == right.pin, "distinct pins pooled despite feasibility check"
        pin = left.pin

    return _Block(
        values=merged,
        pin=pin,
        members=left.members + right.members,
    )


# --------------------------------------------------------------------------- #
# Feasibility
# --------------------------------------------------------------------------- #


def _check_feasibility(cues: List[Cue], min_gap_ms: int, shifts: List[int]) -> List[Conflict]:
    """Verify the locked positions are mutually compatible.

    In shifted space the observations of pinned cues must be non-decreasing,
    and the first fitted value must not fall below 0 (modeled as a virtual
    pin of value 0 immediately before cue 0). Only consecutive pins can
    violate this; free cues between them absorb the slack. Each violation is
    reported in original (millisecond) coordinates.
    """
    conflicts: List[Conflict] = []

    # The y_0 >= 0 boundary is a virtual anchor at index -1 with shifted
    # value 0: the first pin must sit at start >= shifts[its index], i.e.
    # enough room must exist before it for all (free) preceding cues.
    anchors: List[tuple[int, int, Optional[Cue]]] = [(-1, 0, None)]
    for cue in cues:
        if cue.pinned:
            anchors.append((cue.index, cue.start_ms - shifts[cue.index], cue))

    for (prev_idx, prev_val, prev_cue), (cur_idx, cur_val, cur_cue) in zip(
        anchors, anchors[1:]
    ):
        if cur_val >= prev_val:
            continue

        if prev_idx == -1:
            # Earliest legal start of cur is shifts[cur_idx]: all preceding
            # cues packed from time 0 with minimum gaps.
            required = shifts[cur_idx]
            actual = cur_cue.start_ms  # type: ignore[union-attr]
            reason = (
                f"Locked segment {cur_idx} starts at {actual} ms but needs at "
                f"least {required} ms to keep all preceding segments on the "
                f"timeline with the {min_gap_ms} ms minimum gap"
            )
            conflicts.append(
                Conflict(
                    from_index=None,
                    from_id=None,
                    to_index=cur_idx,
                    to_id=cur_cue.cue_id,  # type: ignore[union-attr]
                    reason_code="locked_before_timeline_start",
                    reason=reason,
                    required_start_ms=required,
                    actual_start_ms=actual,
                    shortfall_ms=required - actual,
                )
            )
        else:
            # Earliest legal start of cur relative to the previous anchor.
            span = shifts[cur_idx] - shifts[prev_idx]
            required = prev_cue.start_ms + span  # type: ignore[union-attr]
            actual = cur_cue.start_ms  # type: ignore[union-attr]
            reason = (
                f"Locked segments {prev_idx} and {cur_idx} cannot both keep "
                f"their positions: {span} ms are required between their starts "
                f"but only {actual - prev_cue.start_ms} ms are available "  # type: ignore[union-attr]
                f"(shortfall {required - actual} ms, minimum gap "
                f"{min_gap_ms} ms)"
            )
            conflicts.append(
                Conflict(
                    from_index=prev_idx,
                    from_id=prev_cue.cue_id,  # type: ignore[union-attr]
                    to_index=cur_idx,
                    to_id=cur_cue.cue_id,  # type: ignore[union-attr]
                    reason_code="locked_segments_overlap_or_gap_violation",
                    reason=reason,
                    required_start_ms=required,
                    actual_start_ms=actual,
                    shortfall_ms=required - actual,
                )
            )

    return conflicts


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def normalize(cues: List[Cue], min_gap_ms: int = 0) -> NormalizationResult:
    """Return the minimum-displacement, overlap-free timeline.

    Raises :class:`InfeasibleError` (carrying all detected conflicts) when
    the pinned positions cannot be satisfied simultaneously.
    """
    n = len(cues)

    # shifts[i] = c_i: how much cue i is pushed back by the durations and
    # minimum gaps of all preceding cues, even in the tightest packing.
    shifts: List[int] = [0] * n
    for i in range(1, n):
        shifts[i] = shifts[i - 1] + cues[i - 1].duration_ms + min_gap_ms

    conflicts = _check_feasibility(cues, min_gap_ms, shifts)
    if conflicts:
        raise InfeasibleError(conflicts)

    if n == 0:
        return NormalizationResult(cues=[], total_absolute_displacement_ms=0)

    # PAVA. The y_0 >= 0 boundary is a pinned observation 0 immediately
    # before the first cue; standard PAVA seeding with it is exact. It can
    # only pool into a block whose fit is < 0, and the pooled fit is then
    # the max(0, median), so it never drags a healthy solution downward.
    blocks: List[_Block] = [_Block(values=[], pin=0, members=[-1])]

    for cue, shift in zip(cues, shifts):
        target = cue.start_ms - shift
        if cue.pinned:
            block = _Block(values=[], pin=target, members=[cue.index])
        else:
            block = _Block(values=[target], pin=None, members=[cue.index])
        blocks.append(block)

        while len(blocks) >= 2 and blocks[-2].fit > blocks[-1].fit:
            right = blocks.pop()
            left = blocks.pop()
            blocks.append(_merge_blocks(left, right))

    fitted: dict[int, int] = {}
    for block in blocks:
        for member in block.members:
            if member >= 0:
                fitted[member] = block.fit

    normalized: List[NormalizedCue] = []
    total_displacement = 0
    for cue, shift in zip(cues, shifts):
        new_start = fitted[cue.index] + shift
        new_end = new_start + cue.duration_ms
        displacement = abs(new_start - cue.start_ms)
        total_displacement += displacement
        if cue.pinned:
            # Guaranteed by construction; guard against regression.
            assert new_start == cue.start_ms, "pinned cue was moved"
        normalized.append(
            NormalizedCue(
                index=cue.index,
                cue_id=cue.cue_id,
                text=cue.text,
                start_ms=new_start,
                end_ms=new_end,
                pinned=cue.pinned,
                displacement_ms=displacement,
            )
        )

    return NormalizationResult(
        cues=normalized,
        total_absolute_displacement_ms=total_displacement,
    )
