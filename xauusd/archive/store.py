"""Archive writes. Every one of them idempotent.

The archiver will re-see messages constantly: backfill windows overlap on
purpose, a restart re-scans the most recent block, and the live listener and a
catch-up pass can deliver the same message twice. So "archive this message"
must be safe to call any number of times and must never lose an earlier
version of anything.

Transaction shape
-----------------
One deliberate rule: **no SQLite write lock is ever held across a network
call.** Downloading an attachment can block for seconds on a slow DC, and
holding `BEGIN IMMEDIATE` through it would stall every other writer — including
the outbox, which is how "we archived it" and "we told you" both go quiet at
once.

So the flow is three phases:

    admit()      one transaction: message row + declared media + policy verdicts
      -> returns a download plan
    download     no transaction at all
    complete()   one short transaction per attachment, then finalise the hash

`record_message` returns that plan rather than doing the fetching itself, which
also means the whole decision layer is testable without any I/O.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from xauusd.archive import content as content_mod
from xauusd.archive.media import (
    Admission,
    DeclaredMedia,
    MediaLimits,
    admit,
    relative_path,
)
from xauusd.archive.ranges import IdRange, merge
from xauusd.clock import iso_utc
from xauusd.db.store import write_transaction
from xauusd.telegram.model import Deletion, IncomingMessage

# Media states, mirroring the CHECK constraint in 001_init.sql.
PENDING = "pending"
STORED = "stored"
REJECTED = "rejected"
FAILED = "failed"

# Content states.
COMPLETE = "complete"
PENDING_MEDIA = "pending_media"
MEDIA_FAILED = "media_failed"


@dataclass(frozen=True, slots=True)
class DownloadTask:
    """One attachment admitted for fetching."""

    chat_id: int
    message_id: int
    index: int
    rel_path: PurePosixPath
    declared: DeclaredMedia


@dataclass(frozen=True, slots=True)
class RecordResult:
    """What `record_message` did, for the caller's counters and next steps."""

    inserted: bool
    edit_recorded: bool
    downloads: tuple[DownloadTask, ...]

    @property
    def needs_download(self) -> bool:
        return bool(self.downloads)


def record_message(
    conn: sqlite3.Connection,
    message: IncomingMessage,
    seen_at: datetime,
    limits: MediaLimits,
) -> RecordResult:
    """Store a message and decide what to fetch. Safe to call repeatedly.

    On a re-see with a newer `edited_at`, appends a revision to
    `message_edits` and leaves `messages.text` as first posted — the original
    is the evidence for what trade was specified.
    """
    now = iso_utc(seen_at)
    with write_transaction(conn) as tx:
        existing = tx.execute(
            "SELECT text, edit_count, content_state FROM messages "
            "WHERE chat_id = ? AND message_id = ?",
            (message.chat_id, message.message_id),
        ).fetchone()

        if existing is None:
            tx.execute(
                """
                INSERT INTO messages (
                    chat_id, message_id, posted_at, first_seen_at, sender_id,
                    is_outgoing, is_forwarded, is_service, reply_to_id, grouped_id,
                    text, media_count, content_hash, hash_version, content_state, edit_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, 0)
                """,
                (
                    message.chat_id,
                    message.message_id,
                    iso_utc(message.posted_at),
                    now,
                    message.sender_id,
                    int(message.is_outgoing),
                    int(message.is_forwarded),
                    int(message.is_service),
                    message.reply_to_id,
                    message.grouped_id,
                    message.text,
                    len(message.media),
                    PENDING_MEDIA if message.media else COMPLETE,
                ),
            )
            downloads = _admit_media(tx, message, limits, now)
            # Always finalise, even with attachments present. `_finalise` reads
            # the media rows and decides: pending ones leave the fingerprint
            # NULL, and a message whose every attachment was *rejected* has no
            # download that could ever resolve it, so without this it would stay
            # `pending_media` forever and never become comparable.
            _finalise(tx, message.chat_id, message.message_id)
            return RecordResult(inserted=True, edit_recorded=False, downloads=downloads)

        # Already archived. The only new information a re-see can carry is an
        # edit; media rows for this message already exist and keep their state,
        # so re-scanning never re-downloads what is already stored.
        edit_recorded = False
        if message.edited_at is not None:
            edit_recorded = _record_edit(tx, message, now)
        downloads = _pending_downloads(tx, message, limits)
        return RecordResult(inserted=False, edit_recorded=edit_recorded, downloads=downloads)


def _admit_media(
    tx: sqlite3.Connection,
    message: IncomingMessage,
    limits: MediaLimits,
    now: str,
) -> tuple[DownloadTask, ...]:
    """Insert a row per attachment with its admission verdict."""
    tasks: list[DownloadTask] = []
    for index, declared in enumerate(message.media):
        verdict: Admission = admit(declared, limits)
        if verdict.ok:
            assert declared.mime is not None  # admit() rejects a missing mime
            rel = relative_path(message.chat_id, message.message_id, index, declared.mime)
            tx.execute(
                """
                INSERT INTO media (
                    chat_id, message_id, idx, kind, declared_mime, declared_bytes,
                    declared_w, declared_h, declared_name, state, rel_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.chat_id,
                    message.message_id,
                    index,
                    str(declared.kind),
                    declared.mime,
                    declared.size_bytes,
                    declared.width,
                    declared.height,
                    declared.name,
                    PENDING,
                    str(rel),
                ),
            )
            tasks.append(
                DownloadTask(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    index=index,
                    rel_path=rel,
                    declared=declared,
                )
            )
        else:
            tx.execute(
                """
                INSERT INTO media (
                    chat_id, message_id, idx, kind, declared_mime, declared_bytes,
                    declared_w, declared_h, declared_name, state, reject_reason, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.chat_id,
                    message.message_id,
                    index,
                    str(declared.kind),
                    declared.mime,
                    declared.size_bytes,
                    declared.width,
                    declared.height,
                    declared.name,
                    REJECTED,
                    str(verdict.reason),
                    verdict.detail,
                ),
            )
    return tuple(tasks)


def _pending_downloads(
    tx: sqlite3.Connection, message: IncomingMessage, limits: MediaLimits
) -> tuple[DownloadTask, ...]:
    """Re-issue tasks for attachments still owed bytes, e.g. after a crash."""
    rows = tx.execute(
        "SELECT idx, rel_path, declared_mime, declared_bytes, declared_w, declared_h, "
        "declared_name, kind FROM media "
        "WHERE chat_id = ? AND message_id = ? AND state IN (?, ?) ORDER BY idx",
        (message.chat_id, message.message_id, PENDING, FAILED),
    ).fetchall()
    tasks: list[DownloadTask] = []
    for row in rows:
        index = row["idx"]
        # Prefer the live declaration when the message was re-fetched: file
        # references expire, and the fresh metadata is what a retry must use.
        declared = (
            message.media[index]
            if index < len(message.media)
            else DeclaredMedia(
                kind=row["kind"],
                mime=row["declared_mime"],
                size_bytes=row["declared_bytes"],
                width=row["declared_w"],
                height=row["declared_h"],
                name=row["declared_name"],
            )
        )
        if row["rel_path"] is None:
            continue
        tasks.append(
            DownloadTask(
                chat_id=message.chat_id,
                message_id=message.message_id,
                index=index,
                rel_path=PurePosixPath(row["rel_path"]),
                declared=declared,
            )
        )
    return tuple(tasks)


def _record_edit(tx: sqlite3.Connection, message: IncomingMessage, now: str) -> bool:
    """Append a revision if this edit is new. Returns whether it was recorded."""
    assert message.edited_at is not None
    edited = iso_utc(message.edited_at)
    already = tx.execute(
        "SELECT 1 FROM message_edits WHERE chat_id = ? AND message_id = ? AND edited_at = ?",
        (message.chat_id, message.message_id, edited),
    ).fetchone()
    if already is not None:
        return False

    revision = (
        tx.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM message_edits "
            "WHERE chat_id = ? AND message_id = ?",
            (message.chat_id, message.message_id),
        ).fetchone()[0]
    )
    # The edit's own fingerprint is text-only: an edit notification carries the
    # new body, and re-deriving the media set would need another fetch. So the
    # revision records the text that changed, and the media identity stays on
    # the message row.
    fp = content_mod.fingerprint(message.text)
    tx.execute(
        """
        INSERT INTO message_edits (
            chat_id, message_id, revision, edited_at, seen_at, text, content_hash, hash_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message.chat_id,
            message.message_id,
            revision,
            edited,
            now,
            message.text,
            fp.digest,
            fp.version if fp.comparable else None,
        ),
    )
    tx.execute(
        "UPDATE messages SET edit_count = ? WHERE chat_id = ? AND message_id = ?",
        (revision, message.chat_id, message.message_id),
    )
    return True


def complete_download(
    conn: sqlite3.Connection,
    task: DownloadTask,
    digest: str,
    stored_bytes: int,
    stored_at: datetime,
) -> None:
    """Mark one attachment stored, then re-evaluate the message's fingerprint."""
    with write_transaction(conn) as tx:
        tx.execute(
            """
            UPDATE media
               SET state = ?, sha256 = ?, stored_bytes = ?, stored_at = ?,
                   reject_reason = NULL, last_error = NULL
             WHERE chat_id = ? AND message_id = ? AND idx = ?
            """,
            (
                STORED,
                digest,
                stored_bytes,
                iso_utc(stored_at),
                task.chat_id,
                task.message_id,
                task.index,
            ),
        )
        _finalise(tx, task.chat_id, task.message_id)


def fail_download(
    conn: sqlite3.Connection, task: DownloadTask, error: str, *, permanent: bool
) -> None:
    """Record a failed fetch.

    `permanent=False` leaves the row retryable, so the fingerprint stays NULL
    and the message is not yet comparable. `permanent=True` converts it to a
    policy rejection, which *is* comparable via a stable token — see
    `content.rejected_media_token`.
    """
    with write_transaction(conn) as tx:
        tx.execute(
            """
            UPDATE media
               SET state = ?, attempts = attempts + 1, last_error = ?,
                   reject_reason = COALESCE(reject_reason, ?)
             WHERE chat_id = ? AND message_id = ? AND idx = ?
            """,
            (
                REJECTED if permanent else FAILED,
                error[:500],
                "DOWNLOAD_FAILED" if permanent else None,
                task.chat_id,
                task.message_id,
                task.index,
            ),
        )
        _finalise(tx, task.chat_id, task.message_id)


def reject_download(
    conn: sqlite3.Connection, task: DownloadTask, reason: str, detail: str = ""
) -> None:
    """Reject after download — the bytes contradicted the declaration."""
    with write_transaction(conn) as tx:
        tx.execute(
            """
            UPDATE media
               SET state = ?, reject_reason = ?, last_error = ?, sha256 = NULL,
                   rel_path = NULL, stored_bytes = NULL
             WHERE chat_id = ? AND message_id = ? AND idx = ?
            """,
            (REJECTED, reason, detail[:500], task.chat_id, task.message_id, task.index),
        )
        _finalise(tx, task.chat_id, task.message_id)


def _finalise(tx: sqlite3.Connection, chat_id: int, message_id: int) -> None:
    """Recompute `content_hash` and `content_state` from the media rows.

    The hash is set **only** when every attachment has reached a terminal state.
    While anything is still pending or retryable the hash stays NULL, because a
    message whose identity is not yet computable must not be judged a duplicate
    of anything — and suppressing a real signal as a false duplicate is the
    failure that loses a trade, while the reverse loses money.
    """
    row = tx.execute(
        "SELECT text FROM messages WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchone()
    if row is None:
        return

    media = tx.execute(
        "SELECT idx, state, sha256, reject_reason FROM media "
        "WHERE chat_id = ? AND message_id = ? ORDER BY idx",
        (chat_id, message_id),
    ).fetchall()

    unresolved = [m for m in media if m["state"] in (PENDING, FAILED)]
    if unresolved:
        tx.execute(
            "UPDATE messages SET content_hash = NULL, hash_version = NULL, content_state = ? "
            "WHERE chat_id = ? AND message_id = ?",
            (PENDING_MEDIA, chat_id, message_id),
        )
        return

    tokens: list[str] = []
    any_rejected = False
    for m in media:
        if m["state"] == STORED and m["sha256"]:
            tokens.append(m["sha256"])
        else:
            any_rejected = True
            tokens.append(content_mod.rejected_media_token(m["reject_reason"] or "UNKNOWN"))

    fp = content_mod.fingerprint(row["text"], tuple(tokens))
    tx.execute(
        "UPDATE messages SET content_hash = ?, hash_version = ?, content_state = ? "
        "WHERE chat_id = ? AND message_id = ?",
        (
            fp.digest,
            fp.version if fp.comparable else None,
            MEDIA_FAILED if any_rejected else COMPLETE,
            chat_id,
            message_id,
        ),
    )


def record_deletion(conn: sqlite3.Connection, deletion: Deletion) -> bool:
    """Note that Telegram reported a deletion. Returns whether we knew the message.

    The message row stays. A provider removing a losing call is exactly what
    the archive exists to remember.
    """
    stamp = iso_utc(deletion.noticed_at)
    with write_transaction(conn) as tx:
        known = (
            tx.execute(
                "SELECT 1 FROM messages WHERE chat_id = ? AND message_id = ?",
                (deletion.chat_id, deletion.message_id),
            ).fetchone()
            is not None
        )
        tx.execute(
            "INSERT OR IGNORE INTO message_deletions (chat_id, message_id, noticed_at, was_known) "
            "VALUES (?, ?, ?, ?)",
            (deletion.chat_id, deletion.message_id, stamp, int(known)),
        )
        if known:
            tx.execute(
                "UPDATE messages SET deleted_at = COALESCE(deleted_at, ?) "
                "WHERE chat_id = ? AND message_id = ?",
                (stamp, deletion.chat_id, deletion.message_id),
            )
    return known


# -- coverage ----------------------------------------------------------------


def load_scan_ranges(conn: sqlite3.Connection, chat_id: int) -> tuple[IdRange, ...]:
    rows = conn.execute(
        "SELECT lo, hi FROM scan_ranges WHERE chat_id = ? ORDER BY lo", (chat_id,)
    ).fetchall()
    return tuple(IdRange(r["lo"], r["hi"]) for r in rows)


def add_scan_range(
    conn: sqlite3.Connection, chat_id: int, scanned: IdRange, at: datetime
) -> tuple[IdRange, ...]:
    """Record a scanned interval, re-merging the stored set.

    The whole set is rewritten rather than appended to, which keeps the
    non-overlapping invariant true in the table itself rather than only in the
    code that reads it. The set stays small — merging collapses contiguous scan
    batches — so the rewrite is cheap.
    """
    stamp = iso_utc(at)
    with write_transaction(conn) as tx:
        current = tuple(
            IdRange(r["lo"], r["hi"])
            for r in tx.execute(
                "SELECT lo, hi FROM scan_ranges WHERE chat_id = ? ORDER BY lo", (chat_id,)
            ).fetchall()
        )
        merged = merge((*current, scanned))
        tx.execute("DELETE FROM scan_ranges WHERE chat_id = ?", (chat_id,))
        tx.executemany(
            "INSERT INTO scan_ranges (chat_id, lo, hi, updated_at) VALUES (?, ?, ?, ?)",
            [(chat_id, r.lo, r.hi, stamp) for r in merged],
        )
    return merged


def meta_set(conn: sqlite3.Connection, key: str, value: str, at: datetime) -> None:
    with write_transaction(conn) as tx:
        tx.execute(
            "INSERT INTO archive_meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, iso_utc(at)),
        )


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM archive_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def find_duplicates(
    conn: sqlite3.Connection,
    chat_id: int,
    content_hash: str,
    within_seconds: int,
    now: datetime,
) -> tuple[int, ...]:
    """Message ids in this chat with the same content inside the window (spec §23).

    Returns ids rather than a boolean, so a suspected repost can be logged
    against the message it duplicates.

    The cutoff is computed in Python and compared as text. That is not a style
    choice: SQLite's own `datetime()` renders `2026-09-14 05:45:00` — space
    separator, no `Z`, no microseconds — which does not order correctly against
    the stored `2026-09-14T05:45:00.123456Z`. Comparing the two forms gives a
    dedupe window that is wrong by hours and fails silently. Both sides go
    through `iso_utc`, whose fixed width makes lexicographic order
    chronological.

    Only rows with a computed hash can match: a NULL `content_hash` equals
    nothing in SQL, so a message whose media is still unresolved is never
    reported as a duplicate. That is the fail-closed direction — falsely
    suppressing a real signal costs a trade, and the caller must still refuse to
    act on an incomparable message for the opposite reason.
    """
    if within_seconds <= 0:
        raise ValueError(f"dedupe window must be positive, got {within_seconds}")
    cutoff = iso_utc(now - timedelta(seconds=within_seconds))
    rows = conn.execute(
        "SELECT message_id FROM messages "
        "WHERE chat_id = ? AND content_hash = ? AND posted_at >= ? "
        "ORDER BY message_id",
        (chat_id, content_hash, cutoff),
    ).fetchall()
    return tuple(r["message_id"] for r in rows)
