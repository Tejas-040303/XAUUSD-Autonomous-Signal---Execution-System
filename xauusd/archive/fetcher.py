"""Download an admitted attachment, verify it, and record the outcome.

Split deliberately into transport and verification:

- `record_downloaded` holds every rule about what counts as an acceptable file
  and what gets written to the database. No network.
- `fetch` is the synchronous transport the backfill walk uses.
- the live listener awaits its own download and then calls
  `record_downloaded`.

The split exists because the two paths genuinely need different transports —
backfill is sequential and synchronous, the listener runs inside telethon's
event loop and cannot block it for the seconds a download takes — while the
verification must be *identical* in both. A screenshot fetched during catch-up
and one fetched live have to land in the same place under the same checks, or
P1's corpus is assembled from two different populations and its measured
accuracy means nothing.

Never called inside a database transaction: see `archive/store.py` on why no
write lock is held across a network call.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from xauusd.archive import store
from xauusd.archive.media import MediaLimits, absolute_path, verify_stored
from xauusd.archive.store import DownloadTask
from xauusd.clock import Clock
from xauusd.telegram.model import ChatUnavailable, RateLimited, TelegramReader

# A single unfetchable attachment does not fail a run: the message keeps a NULL
# fingerprint and the row stays on the retry list until this many tries.
MAX_DOWNLOAD_ATTEMPTS = 4

_CHUNK = 1 << 20


def destination_for(media_root: Path, task: DownloadTask) -> Path:
    """Absolute path for this attachment, with its parent created.

    Goes through `absolute_path`, which refuses a path outside the media root —
    defence in depth over `relative_path`, which cannot produce one.
    """
    destination = absolute_path(media_root, task.rel_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def digest_file(path: Path) -> tuple[str, int]:
    """SHA-256 and byte count of what actually landed, read in chunks.

    Chunked rather than `read_bytes()`: the size cap is checked on the
    declaration before download and on the file after, but a broken or hostile
    transfer can still produce a file far larger than declared, and reading it
    whole to measure it is how the archiver gets OOM-killed by a channel post.
    """
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def record_downloaded(
    conn: sqlite3.Connection,
    task: DownloadTask,
    destination: Path,
    *,
    limits: MediaLimits,
    clock: Clock,
) -> bool:
    """Verify a downloaded file and record the result. Returns whether stored.

    The declaration was a claim; these are the bytes. A file that contradicts
    its own metadata is deleted rather than kept, so the media tree never holds
    something the archive describes as rejected — a stale file there would be
    picked up by the corpus builder as if it had passed.
    """
    if not destination.exists():
        store.fail_download(
            conn, task, "download reported success but wrote no file", permanent=False
        )
        return False

    digest, stored_bytes = digest_file(destination)

    verdict = verify_stored(task.declared, stored_bytes, limits)
    if not verdict.ok:
        destination.unlink(missing_ok=True)
        store.reject_download(conn, task, str(verdict.reason), verdict.detail)
        return False

    store.complete_download(conn, task, digest, stored_bytes, clock.now_utc())
    return True


def record_failure(
    conn: sqlite3.Connection, task: DownloadTask, exc: BaseException, attempts_so_far: int
) -> None:
    """Record a transport failure, converting to permanent past the attempt cap."""
    permanent = attempts_so_far + 1 >= MAX_DOWNLOAD_ATTEMPTS
    store.fail_download(conn, task, f"{type(exc).__name__}: {exc}", permanent=permanent)


def fetch(
    reader: TelegramReader,
    conn: sqlite3.Connection,
    task: DownloadTask,
    *,
    media_root: Path,
    limits: MediaLimits,
    clock: Clock,
    attempts_so_far: int = 0,
) -> bool:
    """Synchronous fetch-verify-record, for the backfill walk.

    Re-raises `RateLimited` and `ChatUnavailable`: both mean "stop the whole
    run", and swallowing them here would turn a flood wait into a tight retry
    loop against a server that has just asked us to back off.
    """
    destination = destination_for(media_root, task)
    try:
        reader.download_media(task.chat_id, task.message_id, task.index, destination)
    except (RateLimited, ChatUnavailable):
        raise
    except Exception as exc:  # noqa: BLE001 - any transport error is retryable
        record_failure(conn, task, exc, attempts_so_far)
        return False
    return record_downloaded(conn, task, destination, limits=limits, clock=clock)
