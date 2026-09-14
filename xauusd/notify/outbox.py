"""Transactional outbox for operator-facing messages.

The rule this exists to enforce: **a Telegram outage must never block or delay
execution.** So nothing on the trading path ever calls Telegram. It INSERTs a
row, in the same transaction as the state change it describes, and a separate
loop delivers it. "We moved the stop" and "we said we moved the stop" then
commit together or not at all — no window where the operator was told about
something that got rolled back, and none where a stop moved silently.

Delivery is at-least-once
-------------------------
The Bot API has no client-supplied idempotency key, so a crash between the send
succeeding and the row being marked `sent` must resolve one way or the other.
It resolves toward a duplicate: a repeated `SL_HIT` notice is noise, a missing
one is an operator who does not know their stop was hit.

Two mechanisms keep the queue useful rather than merely complete:

**Priority.** After a ten-minute outage a queue ordered purely by insertion
delivers two hundred stale heartbeats before the `SL_HIT` that happened during
it. Events outrank heartbeats.

**Collapse keys.** A heartbeat is a *snapshot*, so an older pending one carries
no information once a newer exists. Enqueuing with a collapse key replaces the
pending row in place, keeping its queue position. Event messages pass no key
and can never be collapsed away — losing one of those loses the record.

Leases, not flags
-----------------
A boolean `claimed` strands a row forever when the drainer dies mid-send. A
lease expiry means exactly one reclaim happens once it runs out, and recovery
needs no operator intervention.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum

from xauusd.clock import iso_utc, parse_iso_utc
from xauusd.db.store import write_transaction

PENDING = "pending"
CLAIMED = "claimed"
SENT = "sent"
FAILED = "failed"


class Priority(IntEnum):
    """Lower delivers first.

    The ordering is the point: an operator seeing `SL_HIT` forty minutes late
    because heartbeats were queued ahead of it has been actively misled, which
    is worse than not being told at all.
    """

    CRITICAL = 0  # SL_HIT, kill switch tripped, reconciliation mismatch
    EVENT = 10    # BE_MOVED, TP1_FILLED, TP2_FILLED, FINAL_TP, rejections
    STATUS = 20   # archiver progress, startup/shutdown
    HEARTBEAT = 30  # the 3-minute position update; collapsible by definition


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """A claimed row, ready to send."""

    id: int
    chat_id: int
    kind: str
    body: str
    priority: int
    attempts: int


def enqueue(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    kind: str,
    body: str,
    at: datetime,
    priority: Priority | int = Priority.EVENT,
    collapse_key: str | None = None,
    delay_seconds: float = 0.0,
) -> int:
    """Queue a message. Returns its row id.

    With a `collapse_key`, an existing *pending* row for the same
    `(chat_id, collapse_key)` is overwritten in place and its id returned, so a
    stalled queue cannot accumulate stale snapshots. Its original queue position
    is kept deliberately — a heartbeat that keeps jumping to the back of the
    queue starves.

    Callers hold their own transaction in the normal case (that is the whole
    point of an outbox), so this participates in one if it is already open
    rather than forcing its own.
    """
    now = iso_utc(at)
    next_attempt = iso_utc(at + timedelta(seconds=delay_seconds))

    def _write(tx: sqlite3.Connection) -> int:
        if collapse_key is not None:
            updated = tx.execute(
                """
                UPDATE outbox
                   SET body = ?, kind = ?, priority = ?, created_at = ?, next_attempt_at = ?
                 WHERE chat_id = ? AND collapse_key = ? AND state = ?
                """,
                (body, kind, int(priority), now, next_attempt, chat_id, collapse_key, PENDING),
            )
            if updated.rowcount:
                row = tx.execute(
                    "SELECT id FROM outbox WHERE chat_id = ? AND collapse_key = ? AND state = ?",
                    (chat_id, collapse_key, PENDING),
                ).fetchone()
                return int(row["id"])
        cursor = tx.execute(
            """
            INSERT INTO outbox (
                created_at, chat_id, kind, priority, body, collapse_key,
                state, attempts, next_attempt_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (now, chat_id, kind, int(priority), body, collapse_key, PENDING, next_attempt),
        )
        return int(cursor.lastrowid)

    if conn.in_transaction:
        return _write(conn)
    with write_transaction(conn) as tx:
        return _write(tx)


def claim(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    lease_seconds: float = 30.0,
    limit: int = 10,
) -> tuple[OutboxMessage, ...]:
    """Take up to `limit` deliverable messages, leasing them.

    Eligible rows are `pending` past their `next_attempt_at`, plus `claimed`
    rows whose lease has expired — that second case is what makes a killed
    drainer self-healing.

    Ordered by priority then id. **Run exactly one drainer.** Two would each get
    disjoint rows (the claim is atomic) but could deliver them out of order, and
    `TP1_FILLED` arriving after `FINAL_TP` reads as a different trade.
    """
    stamp = iso_utc(now)
    lease_until = iso_utc(now + timedelta(seconds=lease_seconds))
    with write_transaction(conn) as tx:
        rows = tx.execute(
            """
            SELECT id, chat_id, kind, body, priority, attempts FROM outbox
             WHERE (state = ? AND next_attempt_at <= ?)
                OR (state = ? AND lease_expires_at <= ?)
             ORDER BY priority, id
             LIMIT ?
            """,
            (PENDING, stamp, CLAIMED, stamp, limit),
        ).fetchall()
        if not rows:
            return ()
        ids = [int(r["id"]) for r in rows]
        tx.executemany(
            "UPDATE outbox SET state = ?, lease_expires_at = ? WHERE id = ?",
            [(CLAIMED, lease_until, i) for i in ids],
        )
        return tuple(
            OutboxMessage(
                id=int(r["id"]),
                chat_id=int(r["chat_id"]),
                kind=str(r["kind"]),
                body=str(r["body"]),
                priority=int(r["priority"]),
                attempts=int(r["attempts"]),
            )
            for r in rows
        )


def mark_sent(conn: sqlite3.Connection, message_id: int, at: datetime) -> None:
    with write_transaction(conn) as tx:
        tx.execute(
            "UPDATE outbox SET state = ?, sent_at = ?, lease_expires_at = NULL, "
            "last_error = NULL WHERE id = ?",
            (SENT, iso_utc(at), message_id),
        )


def mark_failed(
    conn: sqlite3.Connection,
    message_id: int,
    error: str,
    at: datetime,
    *,
    max_attempts: int = 8,
    retry_after_seconds: float | None = None,
    base_backoff_seconds: float = 2.0,
) -> bool:
    """Record a delivery failure. Returns whether it will be retried.

    Backoff is exponential and capped. `retry_after_seconds` overrides it, for
    a Telegram 429 that names its own wait — ignoring a stated `retry_after`
    earns a longer ban, so the server's number always wins.

    Past `max_attempts` the row becomes `failed`, which is terminal but keeps
    the body. An undelivered `SL_HIT` is evidence about the incident; deleting
    it would destroy the only record that the operator was never told.
    """
    with write_transaction(conn) as tx:
        row = tx.execute("SELECT attempts FROM outbox WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            return False
        attempts = int(row["attempts"]) + 1

        if attempts >= max_attempts:
            tx.execute(
                "UPDATE outbox SET state = ?, attempts = ?, last_error = ?, "
                "lease_expires_at = NULL WHERE id = ?",
                (FAILED, attempts, error[:500], message_id),
            )
            return False

        if retry_after_seconds is not None:
            delay = float(retry_after_seconds)
        else:
            delay = min(base_backoff_seconds * (2 ** (attempts - 1)), 300.0)
        tx.execute(
            "UPDATE outbox SET state = ?, attempts = ?, last_error = ?, "
            "next_attempt_at = ?, lease_expires_at = NULL WHERE id = ?",
            (PENDING, attempts, error[:500], iso_utc(at + timedelta(seconds=delay)), message_id),
        )
        return True


def pending_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE state IN (?, ?)", (PENDING, CLAIMED)
        ).fetchone()[0]
    )


def failed_messages(conn: sqlite3.Connection, limit: int = 50) -> tuple[OutboxMessage, ...]:
    """Terminally undelivered messages, newest first. For the startup report.

    Surfaced on purpose: a `failed` row means the operator was not told
    something, and starting a new session without mentioning that hides it.
    """
    rows = conn.execute(
        "SELECT id, chat_id, kind, body, priority, attempts FROM outbox "
        "WHERE state = ? ORDER BY id DESC LIMIT ?",
        (FAILED, limit),
    ).fetchall()
    return tuple(
        OutboxMessage(
            id=int(r["id"]),
            chat_id=int(r["chat_id"]),
            kind=str(r["kind"]),
            body=str(r["body"]),
            priority=int(r["priority"]),
            attempts=int(r["attempts"]),
        )
        for r in rows
    )


def oldest_pending_age_seconds(conn: sqlite3.Connection, now: datetime) -> float | None:
    """Age of the oldest undelivered message, or `None` if the queue is empty.

    The health metric that matters: a growing number here means status messages
    are silently not arriving, and the operator's evidence for "nothing is
    happening" is indistinguishable from "the notifier is broken".
    """
    row = conn.execute(
        "SELECT MIN(created_at) AS oldest FROM outbox WHERE state IN (?, ?)",
        (PENDING, CLAIMED),
    ).fetchone()
    if row is None or row["oldest"] is None:
        return None
    return (now - parse_iso_utc(row["oldest"])).total_seconds()
