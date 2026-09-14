"""Scan-range algebra: the thing that decides whether a signal can be missed.

Coverage is recorded as id intervals actually looked at, because Telegram ids
are not contiguous — deletions and service messages consume them — so "id 4243
is absent from the archive" does not mean it was missed.

The two failure modes under test:

- a **gap reported as covered** loses messages permanently and silently, because
  nothing ever looks at that range again;
- a **hole that cannot be expressed** is what a high-water mark produces, since
  backfill walks history backwards and an interrupted run leaves the newest
  block present with an older block missing.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from xauusd.archive.ranges import (
    IdRange,
    contiguous_from,
    covers,
    gaps,
    max_scanned,
    merge,
    to_telethon_bounds,
)


def r(lo: int, hi: int) -> IdRange:
    return IdRange(lo, hi)


# -- construction ------------------------------------------------------------


def test_empty_range_is_rejected():
    with pytest.raises(ValueError, match="empty range"):
        r(10, 9)


@pytest.mark.parametrize("lo", [0, -1, -1000])
def test_ids_below_one_are_rejected(lo: int):
    """Almost always an uninitialised watermark leaking into the arithmetic."""
    with pytest.raises(ValueError, match="start at 1"):
        IdRange(lo, 100)


# -- merging -----------------------------------------------------------------


def test_adjacent_ranges_merge():
    """[1,5] and [6,9] must become [1,9].

    Without adjacency the table accumulates one row per scan batch, and every
    invariant that reads "non-overlapping" stops meaning anything.
    """
    assert merge([r(1, 5), r(6, 9)]) == (r(1, 9),)


def test_overlapping_ranges_merge():
    assert merge([r(1, 9), r(4, 6), r(20, 30)]) == (r(1, 9), r(20, 30))


def test_merge_is_order_independent():
    assert merge([r(20, 30), r(1, 5), r(6, 9)]) == merge([r(6, 9), r(1, 5), r(20, 30)])


def test_disjoint_ranges_stay_separate():
    assert merge([r(1, 5), r(7, 9)]) == (r(1, 5), r(7, 9))


# -- gaps --------------------------------------------------------------------


def test_gaps_in_the_middle():
    assert gaps([r(1, 10), r(21, 30)], r(1, 40)) == (r(11, 20), r(31, 40))


def test_no_gaps_when_fully_covered():
    assert gaps([r(1, 100)], r(5, 50)) == ()
    assert covers([r(1, 100)], r(5, 50))


def test_everything_is_a_gap_on_a_fresh_chat():
    assert gaps([], r(1, 50)) == (r(1, 50),)


def test_window_entirely_outside_coverage():
    assert gaps([r(100, 200)], r(1, 10)) == (r(1, 10),)
    assert gaps([r(1, 10)], r(100, 200)) == (r(100, 200),)


def test_window_narrower_than_the_hole():
    assert gaps([r(1, 10), r(100, 200)], r(50, 60)) == (r(50, 60),)


# -- the watermark trap ------------------------------------------------------


def test_max_scanned_is_not_a_resume_point():
    """With a hole below it, `max_scanned` reports the top and says nothing
    about completeness. Documented as reporting-only for exactly this reason."""
    holed = [r(1, 10), r(21, 30)]
    assert max_scanned(holed) == 30
    assert gaps(holed, r(1, 30)) == (r(11, 20),)


def test_contiguous_from_stops_at_the_first_hole():
    holed = [r(1, 10), r(21, 30)]
    assert contiguous_from(holed, 1) == 10
    assert contiguous_from(holed, 21) == 30
    assert contiguous_from(holed, 15) is None


def test_max_scanned_of_nothing_is_none():
    assert max_scanned([]) is None


# -- telethon bound conversion ----------------------------------------------


def test_telethon_bounds_are_exclusive():
    """Verified against telethon 1.45: min_id and max_id both EXCLUDE the bound
    ("all the messages with a lower (older) ID or equal to this will be
    excluded"). Getting this wrong drops the first and last message of every
    batch — an evenly spread loss no count check would notice."""
    assert to_telethon_bounds(r(100, 200)) == (99, 201)


def test_telethon_lower_bound_of_one_needs_no_special_case():
    """`min_id=0` is telethon's "no lower bound", which is what lo == 1 means."""
    assert to_telethon_bounds(r(1, 50)) == (0, 51)


# -- properties --------------------------------------------------------------

_ranges = st.lists(
    st.tuples(st.integers(1, 400), st.integers(0, 60)).map(lambda t: IdRange(t[0], t[0] + t[1])),
    max_size=12,
)


@pytest.mark.property
@settings(max_examples=300)
@given(_ranges)
def test_merge_output_is_sorted_disjoint_and_non_adjacent(ranges: list[IdRange]):
    """The invariant the `scan_ranges` table stores. If it can be violated, the
    table can hold two rows claiming the same ids with different bounds."""
    merged = merge(ranges)
    for left, right in zip(merged, merged[1:]):
        assert left.hi + 1 < right.lo, f"{left} and {right} should have merged"


@pytest.mark.property
@settings(max_examples=300)
@given(_ranges)
def test_merge_preserves_membership(ranges: list[IdRange]):
    """No id gains or loses coverage. A merge that widened a range would claim
    coverage of ids never scanned, which is the silent-loss direction."""
    merged = merge(ranges)
    covered_before = {i for rng in ranges for i in range(rng.lo, rng.hi + 1)}
    covered_after = {i for rng in merged for i in range(rng.lo, rng.hi + 1)}
    assert covered_before == covered_after


@pytest.mark.property
@settings(max_examples=300)
@given(_ranges)
def test_merge_is_idempotent(ranges: list[IdRange]):
    once = merge(ranges)
    assert merge(once) == once


@pytest.mark.property
@settings(max_examples=300)
@given(_ranges, st.integers(1, 400), st.integers(0, 120))
def test_gaps_are_exactly_the_uncovered_ids(ranges: list[IdRange], lo: int, span: int):
    """The definition, checked by enumeration rather than by re-deriving it."""
    window = IdRange(lo, lo + span)
    covered = {i for rng in merge(ranges) for i in range(rng.lo, rng.hi + 1)}
    expected = {i for i in range(window.lo, window.hi + 1) if i not in covered}
    reported = {i for hole in gaps(ranges, window) for i in range(hole.lo, hole.hi + 1)}
    assert reported == expected


@pytest.mark.property
@settings(max_examples=300)
@given(_ranges, st.integers(1, 400), st.integers(0, 120))
def test_gaps_lie_inside_the_window(ranges: list[IdRange], lo: int, span: int):
    window = IdRange(lo, lo + span)
    for hole in gaps(ranges, window):
        assert window.lo <= hole.lo <= hole.hi <= window.hi


@pytest.mark.property
@settings(max_examples=300)
@given(_ranges, st.integers(1, 400), st.integers(0, 120))
def test_scanning_the_gaps_closes_them(ranges: list[IdRange], lo: int, span: int):
    """The termination argument for backfill: recording the reported gaps as
    scanned must leave the window complete. Otherwise the walk loops forever on
    a range it believes it has not covered."""
    window = IdRange(lo, lo + span)
    holes = gaps(ranges, window)
    assert covers([*ranges, *holes], window)


@pytest.mark.property
@settings(max_examples=200)
@given(st.integers(1, 10_000), st.integers(0, 500))
def test_bound_conversion_round_trips(lo: int, span: int):
    """An inclusive window converted to exclusive bounds must describe the same
    set of ids: lo-1 < id < hi+1 is exactly lo <= id <= hi."""
    window = IdRange(lo, lo + span)
    min_id, max_id = to_telethon_bounds(window)
    assume(max_id > min_id)
    assert min_id + 1 == window.lo
    assert max_id - 1 == window.hi
