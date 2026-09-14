"""Walk a chat's history and archive what is missing (spec §28).

Two jobs, one mechanism:

- **Corpus building.** P1's parser must be developed against real screenshots,
  not invented examples, so the first run pulls history back as far as
  configured.
- **Restart recovery.** The live listener only sees messages posted while it is
  connected. Everything between the last shutdown and the next connect is a
  hole, and a bot that silently skips a morning's signals is worse than one
  that refuses to start.

Both reduce to the same question — *which id ranges have I not looked at* — and
`archive/ranges.py` answers it. This module turns that answer into batched
fetches and keeps the coverage record honest.

No telethon import: it drives the `TelegramReader` Protocol, so the whole walk
is exercised in tests against a scripted history. That is deliberate. The
interesting behaviour here is the crash-safety ordering, and testing it against
a live MTProto client is not practical.

The ordering rule
-----------------
**Messages are recorded before their range is marked scanned, always.** The two
are separate transactions, so a crash between them re-scans a window that was
already archived — which is harmless, because every archive write is
idempotent. The opposite order would mark coverage for messages that were never
stored, and nothing would ever look at that range again. One direction costs
duplicate work; the other loses signals permanently and silently.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from xauusd.archive import fetcher, store
from xauusd.archive.media import MediaLimits
from xauusd.archive.ranges import IdRange, gaps
from xauusd.clock import Clock
from xauusd.telegram.model import ChatUnavailable, RateLimited, TelegramReader

# How many ids one fetch covers. Bounds both the request size and the amount of
# work redone after a crash.
DEFAULT_BATCH = 200


@dataclass(slots=True)
class BackfillReport:
    """What a run did. Returned rather than logged, so callers can assert on it."""

    chat_id: int
    windows_planned: int = 0
    windows_scanned: int = 0
    messages_seen: int = 0
    messages_new: int = 0
    edits_recorded: int = 0
    media_stored: int = 0
    media_unresolved: int = 0
    stopped_early: str | None = None
    remaining_gaps: tuple[IdRange, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        return self.stopped_early is None and not self.remaining_gaps


def plan(
    scanned: Sequence[IdRange],
    window: IdRange,
    batch_size: int = DEFAULT_BATCH,
    *,
    newest_first: bool = True,
) -> tuple[IdRange, ...]:
    """Split the unscanned parts of `window` into fetchable batches.

    `newest_first` because an interrupted first backfill should leave the *most
    recent* history present: recent screenshots are the ones that match the
    provider's current formatting, and a corpus of two-year-old layouts trains
    the parser on a format that has since changed.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be positive, got {batch_size}")

    batches: list[IdRange] = []
    for hole in gaps(scanned, window):
        lo = hole.lo
        while lo <= hole.hi:
            hi = min(lo + batch_size - 1, hole.hi)
            batches.append(IdRange(lo, hi))
            lo = hi + 1
    if newest_first:
        batches.reverse()
    return tuple(batches)


def run(
    reader: TelegramReader,
    conn: sqlite3.Connection,
    chat_id: int,
    *,
    clock: Clock,
    limits: MediaLimits,
    media_root: Path,
    oldest_message_id: int = 1,
    batch_size: int = DEFAULT_BATCH,
    max_windows: int | None = None,
    on_progress: Callable[[BackfillReport], None] | None = None,
) -> BackfillReport:
    """Archive every message in `[oldest_message_id, newest]` not already covered.

    Stops early — without marking anything it did not finish — on a rate limit
    or an unreadable chat, recording why in the report. `max_windows` bounds one
    invocation so a first run against years of history can be done in sessions
    rather than one enormous transaction-free march.
    """
    report = BackfillReport(chat_id=chat_id)

    try:
        newest = reader.newest_message_id(chat_id)
    except RateLimited as exc:
        report.stopped_early = f"rate limited before starting: {exc.retry_after_seconds:.0f}s"
        return report
    except ChatUnavailable as exc:
        report.stopped_early = f"chat unavailable: {exc}"
        return report

    if newest is None:
        # An empty chat is fully covered by definition. Recording nothing is
        # correct: a scan range would later claim coverage of ids that do not
        # exist yet and suppress the first real message.
        report.remaining_gaps = ()
        return report

    window = IdRange(oldest_message_id, newest)
    already = store.load_scan_ranges(conn, chat_id)
    windows = plan(already, window, batch_size)
    report.windows_planned = len(windows)
    if max_windows is not None:
        windows = windows[:max_windows]

    for batch in windows:
        try:
            messages = list(reader.fetch_range(chat_id, batch))
        except RateLimited as exc:
            report.stopped_early = f"rate limited: wait {exc.retry_after_seconds:.0f}s"
            break
        except ChatUnavailable as exc:
            report.stopped_early = f"chat unavailable: {exc}"
            break
        except Exception as exc:  # noqa: BLE001
            # The batch is left unmarked, so the next run retries exactly it.
            report.stopped_early = f"{type(exc).__name__}: {exc}"
            break

        tasks: list[store.DownloadTask] = []
        for message in messages:
            result = store.record_message(conn, message, clock.now_utc(), limits)
            report.messages_seen += 1
            report.messages_new += int(result.inserted)
            report.edits_recorded += int(result.edit_recorded)
            tasks.extend(result.downloads)

        # Media is fetched after the message rows are committed, so a failure
        # here leaves a recorded message with a NULL fingerprint — retryable —
        # rather than an unrecorded message.
        rate_limited: RateLimited | None = None
        for task in tasks:
            try:
                stored = fetcher.fetch(
                    reader, conn, task, media_root=media_root, limits=limits, clock=clock
                )
            except RateLimited as exc:
                rate_limited = exc
                break
            except ChatUnavailable as exc:
                report.stopped_early = f"chat unavailable: {exc}"
                break
            report.media_stored += int(stored)
            report.media_unresolved += int(not stored)

        # The range is marked only now, after every message in it is durably
        # recorded. Unresolved media does not block it: those attachments are
        # tracked per-row in `media` and retried independently, whereas an
        # unmarked range would re-fetch the entire batch to find them.
        store.add_scan_range(conn, chat_id, batch, clock.now_utc())
        report.windows_scanned += 1

        if on_progress is not None:
            on_progress(report)

        if rate_limited is not None:
            report.stopped_early = (
                f"rate limited during media fetch: wait {rate_limited.retry_after_seconds:.0f}s"
            )
            break
        if report.stopped_early:
            break

    report.remaining_gaps = gaps(store.load_scan_ranges(conn, chat_id), window)
    return report


def retry_unresolved_media(
    reader: TelegramReader,
    conn: sqlite3.Connection,
    chat_id: int,
    *,
    clock: Clock,
    limits: MediaLimits,
    media_root: Path,
    limit: int = 50,
) -> int:
    """Re-attempt attachments still owed bytes. Returns how many now stored.

    Separate from the walk because the two fail independently: a transient DC
    error loses one screenshot out of a scanned range, and re-scanning the
    whole range to recover it would re-read hundreds of messages. The `media`
    table is the work list; `messages_incomplete` and `media_unresolved` are
    the indexes that make it cheap.
    """
    rows = conn.execute(
        """
        SELECT m.message_id, m.idx, m.rel_path, m.kind, m.declared_mime, m.declared_bytes,
               m.declared_w, m.declared_h, m.declared_name, m.attempts
          FROM media m
         WHERE m.chat_id = ? AND m.state IN ('pending', 'failed') AND m.rel_path IS NOT NULL
         ORDER BY m.message_id DESC, m.idx
         LIMIT ?
        """,
        (chat_id, limit),
    ).fetchall()

    from pathlib import PurePosixPath

    from xauusd.archive.media import DeclaredMedia

    recovered = 0
    for row in rows:
        task = store.DownloadTask(
            chat_id=chat_id,
            message_id=int(row["message_id"]),
            index=int(row["idx"]),
            rel_path=PurePosixPath(row["rel_path"]),
            declared=DeclaredMedia(
                kind=row["kind"],
                mime=row["declared_mime"],
                size_bytes=row["declared_bytes"],
                width=row["declared_w"],
                height=row["declared_h"],
                name=row["declared_name"],
            ),
        )
        try:
            if fetcher.fetch(
                reader,
                conn,
                task,
                media_root=media_root,
                limits=limits,
                clock=clock,
                attempts_so_far=int(row["attempts"]),
            ):
                recovered += 1
        except (RateLimited, ChatUnavailable):
            break
    return recovered
