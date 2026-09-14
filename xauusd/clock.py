"""The only time source in the system.

Spec §4 mandates a timezone-aware clock and forbids naive local timestamps.
CLAUDE.md narrows that: **store UTC**, derive IST only for session logic. India
has no DST so the IST offset is a fixed +05:30, but the broker's server clock
is typically EET/EEST and *does* shift — which is why broker timestamps are
never compared against this clock without an explicitly measured server offset.

Every other module takes a `Clock` by injection. `tests/test_architecture.py`
asserts that no module outside this one reaches for a wall clock directly, so a
test can freeze time by construction rather than by patching.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")


@runtime_checkable
class Clock(Protocol):
    """Time as the system is allowed to see it."""

    def now_utc(self) -> datetime:
        """Current instant, timezone-aware, in UTC. The canonical form for storage."""

    def now_ist(self) -> datetime:
        """The same instant in IST. For session-window logic only, never for storage."""

    def monotonic_s(self) -> float:
        """Seconds from an arbitrary origin that never moves backwards.

        Separate from ``now_utc`` on purpose: an NTP correction or a restored VM
        snapshot can step the wall clock backwards, and any interval measured
        across that step is wrong. Durations use this; timestamps use ``now_utc``.
        """


class SystemClock:
    """The real clock. The single wall-clock call site in the codebase."""

    __slots__ = ()

    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def now_ist(self) -> datetime:
        return self.now_utc().astimezone(IST)

    def monotonic_s(self) -> float:
        return time.monotonic()


@dataclass
class FakeClock:
    """Injected in tests. Time only moves when a test moves it.

    Chosen over ``freezegun`` deliberately: a global patch cannot reach inside a
    broker library or change the broker's own timestamps, so it would give a
    false sense of control in exactly the place time bugs cost money.
    """

    at: datetime
    _mono: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("FakeClock requires an aware datetime; naive time is a bug (spec §4)")
        self.at = self.at.astimezone(UTC)

    def now_utc(self) -> datetime:
        return self.at

    def now_ist(self) -> datetime:
        return self.at.astimezone(IST)

    def monotonic_s(self) -> float:
        return self._mono

    # -- test controls -------------------------------------------------------

    def advance(self, delta: timedelta) -> None:
        """Move both the wall clock and the monotonic counter forward together."""
        if delta.total_seconds() < 0:
            raise ValueError("advance() moves forward; use set_utc() to jump backwards")
        self.at += delta
        self._mono += delta.total_seconds()

    def set_utc(self, at: datetime) -> None:
        """Jump the wall clock without moving the monotonic counter.

        Models an NTP step or a snapshot restore: wall time changes, elapsed
        time does not. Any interval guard that reads ``now_utc`` instead of
        ``monotonic_s`` is defeated by this, which is the point of having it.
        """
        if at.tzinfo is None:
            raise ValueError("set_utc() requires an aware datetime")
        self.at = at.astimezone(UTC)


# -- pure conversions --------------------------------------------------------
# Free functions, not methods: they convert an instant you already hold and do
# not read the clock, so they are safe to call anywhere.


def to_ist(moment: datetime) -> datetime:
    """Convert an aware instant to IST. Rejects naive input."""
    _require_aware(moment)
    return moment.astimezone(IST)


def to_utc(moment: datetime) -> datetime:
    """Convert an aware instant to UTC. Rejects naive input."""
    _require_aware(moment)
    return moment.astimezone(UTC)


def ist_date(moment: datetime) -> date:
    """The IST calendar date of an instant.

    The mapping that must exist in exactly one place: a naive ``date(ts_utc)``
    is wrong for every instant between 18:30 and 00:00 UTC, which is the whole
    evening session.

    Note this is the *calendar* date, not the trading day. Whether the trading
    day should start at IST midnight or at a configured ``day_start_ist`` is
    unresolved — an evening session configured to end after midnight would roll
    ``DailyState`` mid-session and reset ``evening_consumed``. Config validation
    currently forbids that configuration rather than guessing.
    """
    return to_ist(moment).date()


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"naive datetime is a bug (spec §4): {moment!r}")
