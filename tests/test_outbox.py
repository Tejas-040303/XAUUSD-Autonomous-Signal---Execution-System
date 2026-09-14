"""Outbox: the queue that must not lose or delay an operator notification.

Two properties the trading path depends on:

- enqueueing is a database INSERT, so a Telegram outage can never block or
  delay execution;
- a `SL_HIT` is never queued behind a backlog of heartbeats accumulated during
  that outage, because an operator told about a stop forty minutes late has
  been actively misled.
"""

from __future__ import annotations

import datetime as dt

import pytest

from xauusd.db.store import write_transaction
from xauusd.notify.outbox import (
    FAILED,
    PENDING,
    SENT,
    Priority,
    claim,
    enqueue,
    failed_messages,
    mark_failed,
    mark_sent,
    oldest_pending_age_seconds,
    pending_count,
)

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 9, 14, 5, 45, tzinfo=UTC)
STATUS = -1009876543210


def state_of(conn, row_id: int) -> str:
    return conn.execute("SELECT state FROM outbox WHERE id = ?", (row_id,)).fetchone()["state"]


# -- collapse ----------------------------------------------------------------


def test_heartbeats_collapse_to_the_latest(trading_db):
    """A ten-minute outage at three-minute heartbeats would otherwise deliver a
    backlog of stale position snapshots after recovery."""
    for i in range(5):
        enqueue(
            trading_db,
            chat_id=STATUS,
            kind="HEARTBEAT",
            body=f"update {i}",
            at=T0 + dt.timedelta(minutes=3 * i),
            priority=Priority.HEARTBEAT,
            collapse_key="position:1",
        )
    rows = trading_db.execute("SELECT body FROM outbox WHERE kind = 'HEARTBEAT'").fetchall()
    assert len(rows) == 1
    assert rows[0]["body"] == "update 4"


def test_collapsing_keeps_the_original_queue_position(trading_db):
    """A heartbeat that jumped to the back of the queue on every refresh would
    starve and never be delivered at all."""
    first = enqueue(
        trading_db, chat_id=STATUS, kind="HEARTBEAT", body="a", at=T0,
        priority=Priority.HEARTBEAT, collapse_key="position:1",
    )
    again = enqueue(
        trading_db, chat_id=STATUS, kind="HEARTBEAT", body="b", at=T0,
        priority=Priority.HEARTBEAT, collapse_key="position:1",
    )
    assert first == again


def test_event_messages_never_collapse(trading_db):
    """Losing one of these loses the record."""
    for kind in ("TP1_FILLED", "TP2_FILLED", "FINAL_TP"):
        enqueue(trading_db, chat_id=STATUS, kind=kind, body=kind, at=T0)
    assert pending_count(trading_db) == 3


def test_two_messages_with_the_same_key_in_different_chats_do_not_collapse(trading_db):
    enqueue(trading_db, chat_id=STATUS, kind="HEARTBEAT", body="a", at=T0, collapse_key="k")
    enqueue(trading_db, chat_id=-100111, kind="HEARTBEAT", body="b", at=T0, collapse_key="k")
    assert pending_count(trading_db) == 2


def test_a_claimed_message_is_not_collapsed_into(trading_db):
    """It may already be in flight, so overwriting its body would deliver
    something that was never enqueued."""
    enqueue(trading_db, chat_id=STATUS, kind="HEARTBEAT", body="a", at=T0, collapse_key="k")
    claim(trading_db, now=T0)
    enqueue(trading_db, chat_id=STATUS, kind="HEARTBEAT", body="b", at=T0, collapse_key="k")
    assert trading_db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 2


# -- priority ----------------------------------------------------------------


def test_critical_events_are_delivered_before_heartbeats(trading_db):
    enqueue(
        trading_db, chat_id=STATUS, kind="HEARTBEAT", body="hb", at=T0,
        priority=Priority.HEARTBEAT,
    )
    enqueue(trading_db, chat_id=STATUS, kind="TP1_FILLED", body="tp", at=T0, priority=Priority.EVENT)
    enqueue(
        trading_db, chat_id=STATUS, kind="SL_HIT", body="sl", at=T0, priority=Priority.CRITICAL
    )
    order = [m.kind for m in claim(trading_db, now=T0, limit=10)]
    assert order == ["SL_HIT", "TP1_FILLED", "HEARTBEAT"]


def test_equal_priority_keeps_insertion_order(trading_db):
    """`TP1_FILLED` arriving after `FINAL_TP` reads as a different trade."""
    for kind in ("TP1_FILLED", "TP2_FILLED", "FINAL_TP"):
        enqueue(trading_db, chat_id=STATUS, kind=kind, body=kind, at=T0, priority=Priority.EVENT)
    assert [m.kind for m in claim(trading_db, now=T0, limit=10)] == [
        "TP1_FILLED",
        "TP2_FILLED",
        "FINAL_TP",
    ]


# -- leases ------------------------------------------------------------------


def test_a_claim_is_not_handed_out_twice(trading_db):
    enqueue(trading_db, chat_id=STATUS, kind="X", body="b", at=T0)
    assert len(claim(trading_db, now=T0, lease_seconds=30)) == 1
    assert claim(trading_db, now=T0 + dt.timedelta(seconds=5), lease_seconds=30) == ()


def test_an_expired_lease_is_reclaimed(trading_db):
    """A boolean `claimed` would strand this row forever when the drainer dies
    mid-send. Recovery must need no operator intervention."""
    row_id = enqueue(trading_db, chat_id=STATUS, kind="X", body="b", at=T0)
    claim(trading_db, now=T0, lease_seconds=30)
    reclaimed = claim(trading_db, now=T0 + dt.timedelta(seconds=31), lease_seconds=30)
    assert [m.id for m in reclaimed] == [row_id]


def test_delay_defers_eligibility(trading_db):
    enqueue(trading_db, chat_id=STATUS, kind="X", body="b", at=T0, delay_seconds=60)
    assert claim(trading_db, now=T0) == ()
    assert len(claim(trading_db, now=T0 + dt.timedelta(seconds=60))) == 1


# -- retry and terminal failure ----------------------------------------------


def test_failure_backs_off_exponentially(trading_db):
    row_id = enqueue(trading_db, chat_id=STATUS, kind="X", body="b", at=T0)
    claim(trading_db, now=T0)
    delays = []
    for _ in range(4):
        mark_failed(trading_db, row_id, "timeout", T0, base_backoff_seconds=2.0)
        nxt = trading_db.execute(
            "SELECT next_attempt_at FROM outbox WHERE id = ?", (row_id,)
        ).fetchone()["next_attempt_at"]
        delays.append(nxt)
    assert len(set(delays)) == 4, "each retry must wait longer than the last"
    assert state_of(trading_db, row_id) == PENDING


def test_a_stated_retry_after_overrides_our_backoff(trading_db):
    """Ignoring Telegram's own `retry_after` earns a longer ban, so the
    server's number always wins."""
    row_id = enqueue(trading_db, chat_id=STATUS, kind="X", body="b", at=T0)
    mark_failed(trading_db, row_id, "429", T0, retry_after_seconds=120)
    nxt = trading_db.execute(
        "SELECT next_attempt_at FROM outbox WHERE id = ?", (row_id,)
    ).fetchone()["next_attempt_at"]
    assert nxt.startswith("2026-09-14T05:47:00")


def test_exhausted_attempts_become_terminal_but_keep_the_body(trading_db):
    """An undelivered SL_HIT is evidence about the incident; deleting it would
    destroy the only record that the operator was never told."""
    row_id = enqueue(trading_db, chat_id=STATUS, kind="SL_HIT", body="SL hit at 2645", at=T0)
    retried = [mark_failed(trading_db, row_id, "err", T0, max_attempts=3) for _ in range(3)]
    assert retried == [True, True, False]
    stored = trading_db.execute("SELECT state, body FROM outbox WHERE id = ?", (row_id,)).fetchone()
    assert stored["state"] == FAILED
    assert stored["body"] == "SL hit at 2645"


def test_failed_messages_are_surfaced(trading_db):
    row_id = enqueue(trading_db, chat_id=STATUS, kind="SL_HIT", body="b", at=T0)
    for _ in range(3):
        mark_failed(trading_db, row_id, "err", T0, max_attempts=3)
    assert [m.kind for m in failed_messages(trading_db)] == ["SL_HIT"]


def test_a_failed_message_is_not_reclaimed(trading_db):
    row_id = enqueue(trading_db, chat_id=STATUS, kind="X", body="b", at=T0)
    for _ in range(3):
        mark_failed(trading_db, row_id, "err", T0, max_attempts=3)
    assert claim(trading_db, now=T0 + dt.timedelta(days=1)) == ()


def test_marking_an_unknown_id_failed_is_harmless(trading_db):
    assert mark_failed(trading_db, 999, "err", T0) is False


def test_sent_messages_leave_the_queue(trading_db):
    row_id = enqueue(trading_db, chat_id=STATUS, kind="X", body="b", at=T0)
    claimed = claim(trading_db, now=T0)
    mark_sent(trading_db, claimed[0].id, T0)
    assert state_of(trading_db, row_id) == SENT
    assert pending_count(trading_db) == 0
    assert claim(trading_db, now=T0 + dt.timedelta(days=1)) == ()


# -- transactional participation ---------------------------------------------


def test_enqueue_joins_the_callers_transaction(trading_db):
    """The whole point of an outbox: "we moved the stop" and "we said we moved
    the stop" commit together or not at all."""
    with pytest.raises(RuntimeError):
        with write_transaction(trading_db) as tx:
            enqueue(tx, chat_id=STATUS, kind="BE_MOVED", body="moved", at=T0)
            raise RuntimeError("the state change failed after we queued the notice")
    assert pending_count(trading_db) == 0


def test_enqueue_commits_when_the_callers_transaction_does(trading_db):
    with write_transaction(trading_db) as tx:
        enqueue(tx, chat_id=STATUS, kind="BE_MOVED", body="moved", at=T0)
    assert pending_count(trading_db) == 1


# -- health ------------------------------------------------------------------


def test_oldest_pending_age_tracks_the_queue(trading_db):
    assert oldest_pending_age_seconds(trading_db, T0) is None
    enqueue(trading_db, chat_id=STATUS, kind="X", body="b", at=T0)
    age = oldest_pending_age_seconds(trading_db, T0 + dt.timedelta(minutes=25))
    assert age == pytest.approx(1500.0)


def test_age_ignores_delivered_messages(trading_db):
    row_id = enqueue(trading_db, chat_id=STATUS, kind="X", body="b", at=T0)
    mark_sent(trading_db, row_id, T0)
    assert oldest_pending_age_seconds(trading_db, T0 + dt.timedelta(hours=1)) is None
