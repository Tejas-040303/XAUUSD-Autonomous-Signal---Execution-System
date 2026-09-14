"""The time source.

Session-window *logic* belongs to the policy layer (P2). What is tested here is
the foundation it will stand on: that UTC is what gets stored, that IST is
derived correctly, and that the IST calendar date is right for the instants
where a naive conversion goes wrong — which is the entire evening session.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from xauusd.clock import IST, UTC, Clock, FakeClock, SystemClock, ist_date, to_ist, to_utc


def test_system_clock_satisfies_the_protocol():
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FakeClock(datetime(2026, 9, 14, tzinfo=UTC)), Clock)


def test_system_clock_returns_aware_utc():
    now = SystemClock().now_utc()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_ist_is_utc_plus_530_and_has_no_dst():
    """India observes no DST, so the offset is fixed all year.

    The DST risk in this system is the *broker's* clock (typically EET/EEST),
    which is why broker timestamps are never compared against this one without
    a measured server offset.
    """
    for month in range(1, 13):
        moment = datetime(2026, month, 15, 12, 0, tzinfo=UTC)
        assert to_ist(moment).utcoffset() == timedelta(hours=5, minutes=30)


def test_fake_clock_rejects_naive_input():
    with pytest.raises(ValueError, match="aware datetime"):
        FakeClock(datetime(2026, 9, 14, 5, 30))  # noqa: DTZ001 — deliberately naive


@pytest.mark.parametrize("fn", [to_ist, to_utc, ist_date])
def test_conversions_reject_naive_input(fn):
    with pytest.raises(ValueError, match="naive datetime is a bug"):
        fn(datetime(2026, 9, 14, 5, 30))  # noqa: DTZ001


# ---------------------------------------------------------------------------
# The IST date boundary — where a naive date(ts_utc) is wrong
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("utc_instant", "expected_ist_date"),
    [
        # 18:29 UTC is still the same IST day (23:59 IST).
        (datetime(2026, 9, 14, 18, 29, tzinfo=UTC), date(2026, 9, 14)),
        # 18:30 UTC is already the NEXT IST day (00:00 IST) — this is the case a
        # naive date(ts_utc) gets wrong, and it covers the whole evening session.
        (datetime(2026, 9, 14, 18, 30, tzinfo=UTC), date(2026, 9, 15)),
        (datetime(2026, 9, 14, 23, 59, tzinfo=UTC), date(2026, 9, 15)),
        (datetime(2026, 9, 14, 0, 0, tzinfo=UTC), date(2026, 9, 14)),
    ],
)
def test_ist_date_crosses_at_1830_utc(utc_instant: datetime, expected_ist_date: date):
    assert ist_date(utc_instant) == expected_ist_date


def test_naive_date_extraction_would_be_wrong_for_the_evening_session():
    """Demonstrates the bug this helper exists to prevent."""
    evening = datetime(2026, 9, 14, 20, 30, tzinfo=IST)  # 15:00 UTC — same day
    late_evening = datetime(2026, 9, 15, 0, 30, tzinfo=IST)  # 19:00 UTC on the 14th

    assert ist_date(late_evening) == date(2026, 9, 15)
    # The naive reading would attribute it to the 14th:
    assert to_utc(late_evening).date() == date(2026, 9, 14)
    assert ist_date(evening) == date(2026, 9, 14)


# ---------------------------------------------------------------------------
# §39 session boundary instants
# ---------------------------------------------------------------------------

# Spec §39 lists these IST wall-clock times as required boundary cases. The
# policy layer will decide accept/reject; here we only assert that each maps to
# an unambiguous UTC instant and back, so a boundary test cannot be flaky.
SESSION_BOUNDARIES = [
    time(5, 29),
    time(5, 30),  # morning opens
    time(8, 59),
    time(9, 0),  # morning closes
    time(19, 59),
    time(20, 0),  # evening opens
    time(23, 59),  # the last instant before the IST date rolls
    time(0, 0),  # and the first after it
]


@pytest.mark.parametrize("wall", SESSION_BOUNDARIES, ids=[t.isoformat() for t in SESSION_BOUNDARIES])
def test_session_boundary_instants_round_trip(wall: time):
    ist_moment = datetime.combine(date(2026, 9, 14), wall, tzinfo=IST)
    assert to_ist(to_utc(ist_moment)) == ist_moment


def test_boundary_precision_is_preserved_to_the_microsecond():
    """A test written as time(8, 59) never exercises 08:59:59.999999.

    Whether the morning window is half-open matters at exactly one instant, so
    the clock must not quietly truncate sub-second precision.
    """
    almost_nine = datetime(2026, 9, 14, 8, 59, 59, 999999, tzinfo=IST)
    nine = datetime(2026, 9, 14, 9, 0, 0, 0, tzinfo=IST)
    assert to_utc(almost_nine) < to_utc(nine)
    assert to_ist(to_utc(almost_nine)).microsecond == 999999


# ---------------------------------------------------------------------------
# FakeClock behaviour, including the NTP-step case
# ---------------------------------------------------------------------------


def test_fake_clock_advance_moves_wall_and_monotonic_together():
    clock = FakeClock(datetime(2026, 9, 14, 5, 30, tzinfo=UTC))
    before_mono = clock.monotonic_s()
    clock.advance(timedelta(seconds=90))
    assert clock.now_utc() == datetime(2026, 9, 14, 5, 31, 30, tzinfo=UTC)
    assert clock.monotonic_s() == before_mono + 90


def test_fake_clock_advance_refuses_to_go_backwards():
    clock = FakeClock(datetime(2026, 9, 14, tzinfo=UTC))
    with pytest.raises(ValueError, match="moves forward"):
        clock.advance(timedelta(seconds=-1))


def test_wall_clock_step_does_not_move_monotonic():
    """Models an NTP correction or a restored VM snapshot.

    Any interval guard reading wall time — a rate limiter, say — is defeated by
    this. Reading `monotonic_s` is not. Having the fake able to reproduce it is
    the point: the §24 limiter must be testable against a clock that jumps.
    """
    clock = FakeClock(datetime(2026, 9, 14, 5, 30, tzinfo=UTC))
    clock.advance(timedelta(seconds=30))
    mono_before = clock.monotonic_s()

    clock.set_utc(datetime(2026, 9, 14, 5, 0, tzinfo=UTC))  # wall time jumps back

    assert clock.now_utc() < datetime(2026, 9, 14, 5, 30, tzinfo=UTC)
    assert clock.monotonic_s() == mono_before, "monotonic time must not move on a wall-clock step"


def test_fake_clock_normalises_input_to_utc():
    clock = FakeClock(datetime(2026, 9, 14, 11, 0, tzinfo=IST))
    assert clock.now_utc().utcoffset() == timedelta(0)
    assert clock.now_utc() == datetime(2026, 9, 14, 5, 30, tzinfo=UTC)
    assert clock.now_ist().hour == 11
