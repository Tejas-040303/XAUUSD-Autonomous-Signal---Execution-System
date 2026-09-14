"""Which message ids have actually been scanned.

The problem
-----------
Spec §28 requires backfilling history and, after any restart, knowing that
nothing was missed. The obvious approach — "store the highest message id seen,
fetch everything above it" — fails in two directions:

1. **Telegram message ids are not contiguous.** Deletions, service messages
   (joins, pins) and messages in other topics all consume ids. So "id 4243 is
   absent from the archive" does not mean it was missed; it may not exist. A
   gap detector built on missing ids raises a permanent, unfixable alarm.
2. **A high-water mark cannot express a hole.** Backfill walks history
   *backwards* from the newest message, so an interrupted backfill leaves the
   newest block present and an older block missing. A single watermark says
   "scanned up to the newest", which is true and useless.

So the archive records **closed intervals of ids it has actually looked at**,
merged and non-overlapping. The complement of that set, within any window of
interest, is the work still to do. What was never scanned is then distinguished
from what was scanned and found not to exist, which is the distinction the
whole thing turns on.

Both ends are inclusive here. Telethon's `min_id`/`max_id` are **exclusive**
(verified against the library: "all the messages with a lower (older) ID or
equal to this will be excluded"), so the conversion at that boundary is
off-by-one on purpose — see `to_telethon_bounds`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True, order=True, slots=True)
class IdRange:
    """A closed interval of message ids, `lo <= hi`, both inclusive."""

    lo: int
    hi: int

    def __post_init__(self) -> None:
        if self.lo > self.hi:
            raise ValueError(f"empty range: lo={self.lo} > hi={self.hi}")
        if self.lo < 1:
            # Telegram message ids start at 1. A 0 or negative bound is almost
            # always an uninitialised watermark leaking into the arithmetic.
            raise ValueError(f"message ids start at 1, got lo={self.lo}")

    @property
    def count(self) -> int:
        return self.hi - self.lo + 1

    def contains(self, message_id: int) -> bool:
        return self.lo <= message_id <= self.hi

    def touches(self, other: IdRange) -> bool:
        """Overlaps or is exactly adjacent.

        Adjacency counts: [1,5] and [6,9] must merge into [1,9], otherwise the
        table accumulates one row per scan batch and the merged-invariant tests
        stop meaning anything.
        """
        return self.lo <= other.hi + 1 and other.lo <= self.hi + 1


def merge(ranges: Iterable[IdRange]) -> tuple[IdRange, ...]:
    """Normalise to a sorted, non-overlapping, non-adjacent set.

    This is the archive's stored form, and the invariant every write restores.
    """
    ordered = sorted(ranges)
    if not ordered:
        return ()
    out: list[IdRange] = [ordered[0]]
    for candidate in ordered[1:]:
        last = out[-1]
        if last.touches(candidate):
            out[-1] = IdRange(last.lo, max(last.hi, candidate.hi))
        else:
            out.append(candidate)
    return tuple(out)


def gaps(scanned: Sequence[IdRange], window: IdRange) -> tuple[IdRange, ...]:
    """The parts of `window` that have not been scanned yet.

    This is the backfill work list. It is the set complement, so an empty result
    means genuinely complete coverage of the window rather than "nothing found".
    """
    merged = merge(scanned)
    out: list[IdRange] = []
    cursor = window.lo
    for block in merged:
        if block.hi < cursor:
            continue
        if block.lo > window.hi:
            break
        if block.lo > cursor:
            out.append(IdRange(cursor, min(block.lo - 1, window.hi)))
        cursor = max(cursor, block.hi + 1)
        if cursor > window.hi:
            break
    if cursor <= window.hi:
        out.append(IdRange(cursor, window.hi))
    return tuple(out)


def covers(scanned: Sequence[IdRange], window: IdRange) -> bool:
    return not gaps(scanned, window)


def max_scanned(scanned: Sequence[IdRange]) -> int | None:
    """The highest id ever scanned, or `None` if nothing has been.

    For reporting and for bounding the *top* of a backfill window. **Not a
    resume point.** With a hole at [40,50] and coverage to 900 this returns 900,
    and "resume above 900" is precisely how that hole becomes permanent. Every
    coverage question goes through `gaps`, which is the only function that can
    tell "never scanned" from "scanned, does not exist".
    """
    merged = merge(scanned)
    return merged[-1].hi if merged else None


def contiguous_from(scanned: Sequence[IdRange], start: int) -> int | None:
    """Top of the unbroken run containing `start`, or `None` if it is unscanned.

    The honest form of "how far is the archive complete from here": it stops at
    the first hole instead of stepping over it.
    """
    for block in merge(scanned):
        if block.contains(start):
            return block.hi
    return None


def to_telethon_bounds(window: IdRange) -> tuple[int, int]:
    """`(min_id, max_id)` for `iter_messages`, converted from inclusive bounds.

    Telethon excludes both bounds, so an inclusive `[lo, hi]` becomes
    `min_id = lo - 1`, `max_id = hi + 1`. Getting this wrong drops exactly the
    first and last message of every batch — a silent, evenly-spread loss that
    no count check would notice.

    `min_id=0` is telethon's "no lower bound", which is what `lo == 1` means
    anyway, so the arithmetic needs no special case.
    """
    return window.lo - 1, window.hi + 1
