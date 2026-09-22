"""Unit tests for the PAVA-based timeline solver, including optimality
checks against an exhaustive dynamic program."""

from __future__ import annotations

import random
from typing import List, Optional, Tuple

import pytest

from app.solver import Conflict, Cue, InfeasibleError, normalize


def make_cues(
    starts_ends: List[Tuple[int, int]],
    pinned: Optional[set[int]] = None,
    texts: Optional[List[str]] = None,
) -> List[Cue]:
    pinned = pinned or set()
    return [
        Cue(
            index=i,
            cue_id=f"c{i}",
            text=(texts[i] if texts else f"text {i}"),
            start_ms=s,
            end_ms=e,
            pinned=i in pinned,
        )
        for i, (s, e) in enumerate(starts_ends)
    ]


def assert_valid(result, cues, min_gap_ms):
    """Structural invariants every feasible output must satisfy."""
    assert len(result.cues) == len(cues)
    prev_end = 0
    for out, cue in zip(result.cues, cues):
        assert out.text == cue.text, "text must be preserved"
        assert out.end_ms - out.start_ms == cue.end_ms - cue.start_ms, (
            "duration must be preserved"
        )
        assert out.start_ms >= 0
        if cue.pinned:
            assert out.start_ms == cue.start_ms, "pinned cue must not move"
        if out.index > 0:
            assert out.start_ms >= prev_end + min_gap_ms, "gap/overlap violation"
        prev_end = out.end_ms
        assert out.displacement_ms == abs(out.start_ms - cue.start_ms)
    total = sum(abs(c.start_ms - q.start_ms) for c, q in zip(cues, result.cues))
    assert total == result.total_absolute_displacement_ms


def optimal_displacement_dp(cues: List[Cue], min_gap_ms: int) -> Optional[int]:
    """Brute-force optimum over a finite y grid (L1 median lies on an obs).

    Returns None when no feasible assignment exists.
    """
    n = len(cues)
    shifts = [0] * n
    for i in range(1, n):
        shifts[i] = shifts[i - 1] + cues[i - 1].duration_ms + min_gap_ms

    obs = [0]  # virtual boundary
    for cue, shift in zip(cues, shifts):
        obs.append(cue.start_ms - shift)
    grid = sorted(set(obs))

    # dp[y] = min cost after processing prefix, last y == grid[y].
    # Initialization encodes the y_0 >= 0 boundary with zero cost (no
    # observation precedes cue 0), so any grid point >= 0 is reachable.
    inf = float("inf")
    dp = [0 if grid[y] >= 0 else inf for y in range(len(grid))]
    for i, (cue, shift) in enumerate(zip(cues, shifts)):
        pinned_y = cue.start_ms - shift if cue.pinned else None
        new_dp: list[float] = []
        running_min = inf
        for y, val in enumerate(grid):
            running_min = min(running_min, dp[y])
            if pinned_y is not None and val != pinned_y:
                new_dp.append(inf)
                continue
            cost = inf if running_min == inf else running_min + abs(val - (cue.start_ms - shift))
            new_dp.append(cost)
        dp = new_dp

    best = min(dp)
    return None if best == inf else int(best)


def test_no_overlap_input_is_unchanged():
    cues = make_cues([(0, 500), (600, 1100), (1200, 1700)])
    result = normalize(cues, min_gap_ms=0)
    assert_valid(result, cues, 0)
    assert [c.start_ms for c in result.cues] == [0, 600, 1200]
    assert result.total_absolute_displacement_ms == 0


def test_simple_overlap_optimal_spread():
    # Two 1000 ms cues both starting at 1000; gap 0.
    cues = make_cues([(1000, 2000), (1000, 2000)])
    result = normalize(cues, min_gap_ms=0)
    assert_valid(result, cues, 0)
    assert result.cues[1].start_ms == result.cues[0].end_ms
    assert result.total_absolute_displacement_ms == 1000
    assert result.total_absolute_displacement_ms == optimal_displacement_dp(cues, 0)


def test_min_gap_is_enforced():
    cues = make_cues([(0, 100), (50, 150), (120, 220)])
    result = normalize(cues, min_gap_ms=40)
    assert_valid(result, cues, 40)
    assert result.cues[1].start_ms >= result.cues[0].end_ms + 40
    assert result.cues[2].start_ms >= result.cues[1].end_ms + 40
    assert result.total_absolute_displacement_ms == optimal_displacement_dp(cues, 40)


def test_pinned_cue_stays_put():
    # Feasible lock: cue0 [0,100], cue1 pinned at 250, cue2 overlapping cue1.
    cues = make_cues([(0, 100), (250, 350), (300, 400)], pinned={1})
    result = normalize(cues, min_gap_ms=0)
    assert_valid(result, cues, 0)
    assert result.cues[1].start_ms == 250
    assert result.cues[0].end_ms <= 250
    assert result.cues[2].start_ms >= result.cues[1].end_ms


def test_pinned_first_cue_conflicts_with_neighbor_lock():
    # cue 0 locked at 0, cue 1 locked at 50: 100 ms needed between starts.
    # (A lock ordering problem must use locks in chronological input order;
    # a later lock before the first lock is a boundary violation instead.)
    cues = make_cues([(0, 100), (50, 150)], pinned={0, 1})
    with pytest.raises(InfeasibleError) as exc:
        normalize(cues, min_gap_ms=0)
    c = exc.value.conflicts[0]
    assert c.from_index == 0
    assert c.to_index == 1
    assert c.reason_code == "locked_segments_overlap_or_gap_violation"
    assert c.required_start_ms == 100
    assert c.shortfall_ms == 50


def test_free_cues_absorb_pin():
    # Cue 1 pinned at 100 leaves exactly enough room for the free 100 ms
    # cue 0: feasible with zero displacement.
    cues = make_cues([(0, 100), (100, 200)], pinned={1})
    result = normalize(cues, min_gap_ms=0)
    assert_valid(result, cues, 0)
    assert result.total_absolute_displacement_ms == 0


def test_pinned_conflict_returns_conflicts():
    # cue0 [0,100] pinned, cue1 at 50 pinned: only 50 ms but 100 required.
    cues = make_cues([(0, 100), (50, 150)], pinned={0, 1})
    with pytest.raises(InfeasibleError) as exc:
        normalize(cues, min_gap_ms=0)
    conflicts: List[Conflict] = exc.value.conflicts
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.reason_code == "locked_segments_overlap_or_gap_violation"
    assert c.from_index == 0
    assert c.to_index == 1
    assert c.required_start_ms == 100
    assert c.actual_start_ms == 50
    assert c.shortfall_ms == 50


def test_pinned_before_zero_conflict():
    # Cue 1 locked at 50 but packing the free 100 ms cue 0 before it needs
    # the lock to start at >= 100. There is no "before 0" room, so infeasible.
    cues = make_cues([(0, 100), (50, 150)], pinned={1})
    with pytest.raises(InfeasibleError) as exc:
        normalize(cues, min_gap_ms=0)
    c = exc.value.conflicts[0]
    assert c.from_index is None
    assert c.to_index == 1
    assert c.reason_code == "locked_before_timeline_start"
    assert c.required_start_ms == 100
    assert c.actual_start_ms == 50
    assert c.shortfall_ms == 50


def test_pinned_first_cue_boundary_sanity():
    # A locked cue 0 with start_ms >= 0 is always feasible against the
    # boundary; sanity-check that the boundary anchor does not false-positive.
    cues = make_cues([(0, 100), (0, 100)], pinned={0})
    result = normalize(cues, min_gap_ms=0)
    assert_valid(result, cues, 0)
    assert result.cues[0].start_ms == 0
    assert result.cues[1].start_ms == 100


def test_min_gap_conflict_between_locks():
    # 50 ms between lock starts, durations need 100 + 20 gap = 120.
    cues = make_cues([(0, 100), (50, 150)], pinned={0, 1})
    with pytest.raises(InfeasibleError) as exc:
        normalize(cues, min_gap_ms=20)
    c = exc.value.conflicts[0]
    assert c.required_start_ms == 120
    assert c.shortfall_ms == 70


def test_empty_input():
    result = normalize([], min_gap_ms=10)
    assert result.cues == []
    assert result.total_absolute_displacement_ms == 0


def test_single_cue_never_moves():
    cues = make_cues([(500, 900)])
    result = normalize(cues, min_gap_ms=0)
    assert result.cues[0].start_ms == 500
    assert result.total_absolute_displacement_ms == 0


def test_texts_and_durations_preserved():
    cues = make_cues(
        [(10, 110), (50, 500), (400, 460)],
        texts=["hello", "world", "bye"],
    )
    result = normalize(cues, min_gap_ms=5)
    assert_valid(result, cues, 5)
    assert [c.text for c in result.cues] == ["hello", "world", "bye"]


def test_chain_pushed_right():
    # All four cues start at 1000; no pins. Lower-median isotonic regression
    # with the y_0>=0 boundary pools all four (observations 1000,900,800,700)
    # plus the boundary 0: the lower median is 800 -> starts 800..1100,
    # cost 500 (better than packing right which costs 600).
    cues = make_cues([(1000, 1100)] * 4)
    result = normalize(cues, min_gap_ms=0)
    assert_valid(result, cues, 0)
    assert [c.start_ms for c in result.cues] == [800, 900, 1000, 1100]
    assert result.total_absolute_displacement_ms == 200 + 100 + 0 + 100


def test_random_instances_match_dp_optimum():
    rng = random.Random(42)
    for seed in range(300):
        rng.seed(seed)
        n = rng.randint(1, 6)
        windows = []
        for _ in range(n):
            s = rng.randint(0, 600)
            d = rng.randint(1, 250)
            windows.append((s, s + d))
        gap = rng.randint(0, 50)
        pinned = {i for i in range(n) if rng.random() < 0.3}
        cues = make_cues(windows, pinned=pinned)

        optimum = optimal_displacement_dp(cues, gap)

        if optimum is None:
            with pytest.raises(InfeasibleError):
                normalize(cues, gap)
        else:
            result = normalize(cues, min_gap_ms=gap)
            assert_valid(result, cues, gap)
            assert result.total_absolute_displacement_ms == optimum, (
                f"suboptimal for seed {seed}: {windows}, gap={gap}, pins={pinned}"
            )


def test_grid_independence_dp_helper_sanity():
    # With a feasible pin, the optimum values lie on observations; compare
    # against the DP optimum directly (pinned cue 2 at 200 leaves room).
    cues = make_cues([(0, 100), (50, 150), (200, 300)], pinned={2})
    result = normalize(cues, min_gap_ms=0)
    assert_valid(result, cues, 0)
    assert result.total_absolute_displacement_ms == optimal_displacement_dp(cues, 0)


def test_three_way_squeeze_deterministic():
    # Three identical overlapping cues without pins, classic L1 isotonic case.
    # Pooled with boundary 0, lower median of {0,100,0,-100} sorted
    # [-100,0,0,100] is 0 -> starts 0,100,200, cost 200.
    cues = make_cues([(100, 200), (100, 200), (100, 200)])
    result = normalize(cues, min_gap_ms=0)
    assert_valid(result, cues, 0)
    assert [c.start_ms for c in result.cues] == [0, 100, 200]
    assert result.total_absolute_displacement_ms == 200


def test_multiple_independent_conflicts_reported():
    # Pins at 0, 50 (conflict) and a third pin also at 50 (also conflicts
    # with the second since durations need 100 between starts).
    cues = make_cues([(0, 100), (50, 150), (50, 150)], pinned={0, 1, 2})
    with pytest.raises(InfeasibleError) as exc:
        normalize(cues, min_gap_ms=0)
    assert len(exc.value.conflicts) == 2
