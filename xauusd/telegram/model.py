"""The Telegram boundary: plain data, no library.

`ingest.py` is the only module allowed to import telethon, so every other layer
works against the types here. That is not tidiness — it is what makes the
archiver testable at all. A fake `TelegramReader` replays a scripted history in
a unit test; the alternative is mocking an async MTProto client, which mostly
tests the mock.

It also keeps the library's shape out of the schema. Telethon's `Message` has
about fifty fields, most of them irrelevant and some of them
version-dependent; pinning the eleven the archive actually stores means a
telethon upgrade cannot quietly change what gets recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

from xauusd.archive.media import DeclaredMedia
from xauusd.archive.ranges import IdRange


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """One archived message, normalised away from telethon's representation.

    `text` is the message body **verbatim** — never normalised, never trimmed.
    Normalisation happens only inside the content hash; the stored text has to
    be exactly what was posted, because it is the evidence for what trade was
    specified. Media-only messages carry `""`, never `None`, so the column is
    NOT NULL and no consumer has to branch.
    """

    chat_id: int
    message_id: int
    posted_at: datetime
    text: str
    sender_id: int | None = None
    edited_at: datetime | None = None
    reply_to_id: int | None = None
    grouped_id: int | None = None
    is_outgoing: bool = False
    is_forwarded: bool = False
    is_service: bool = False
    media: tuple[DeclaredMedia, ...] = ()

    def __post_init__(self) -> None:
        if self.message_id < 1:
            raise ValueError(f"message ids start at 1, got {self.message_id}")
        # Spec §4 and CLAUDE.md: timezone-aware only. A naive timestamp here
        # would be stored as if it were UTC and silently shift every IST
        # session boundary by the offset.
        _require_aware(self.posted_at, "posted_at")
        if self.edited_at is not None:
            _require_aware(self.edited_at, "edited_at")

    @property
    def has_content(self) -> bool:
        return bool(self.text.strip()) or bool(self.media)


def _require_aware(moment: datetime, label: str) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(
            f"{label} must be timezone-aware; got naive {moment!r}. "
            "Telethon returns UTC-aware datetimes, so a naive value here means "
            "something stripped the tzinfo on the way in."
        )


@dataclass(frozen=True, slots=True)
class Deletion:
    """Telegram told us a message was removed.

    Recorded, never applied. A provider deleting a losing call is exactly the
    kind of thing the archive exists to remember, so the original row stays and
    the deletion becomes an additional fact about it.
    """

    chat_id: int
    message_id: int
    noticed_at: datetime


@runtime_checkable
class TelegramReader(Protocol):
    """The narrow read surface the archiver needs.

    Deliberately synchronous in signature. Telethon's sync facade drives its own
    event loop, and the archiver's work is I/O-bound-but-sequential — one chat,
    rate-limited by the server anyway — so an async interface here would buy
    concurrency the server will not grant and cost testability.
    """

    def newest_message_id(self, chat_id: int) -> int | None:
        """Highest existing message id, or `None` for an empty chat.

        Needed to bound a backfill window: without it there is no `hi` to
        compute gaps against.
        """

    def fetch_range(self, chat_id: int, window: IdRange) -> Iterable[IncomingMessage]:
        """Messages within an **inclusive** id range, oldest first.

        Inclusive on both ends, unlike telethon's own exclusive `min_id`/
        `max_id` — the conversion lives in `ranges.to_telethon_bounds` so the
        off-by-one exists in exactly one place.

        May yield fewer messages than the window spans: ids are not contiguous.
        That is why coverage is tracked as scanned ranges rather than as a
        count of rows.
        """

    def download_media(
        self, chat_id: int, message_id: int, index: int, destination: Path
    ) -> int:
        """Fetch one attachment to `destination`; return bytes written.

        Called only after `media.admit()` has passed. Destination is chosen by
        `media.relative_path`, never by the sender.
        """


@dataclass(slots=True)
class FakeReader:
    """Scripted history for tests. Records what was asked for.

    Lives here rather than in the test tree because `backfill` is specified
    against it: the assertions that matter are about *which ranges were
    requested*, and that only means something if the fake is part of the
    contract.
    """

    messages: dict[int, list[IncomingMessage]] = field(default_factory=dict)
    requested: list[tuple[int, IdRange]] = field(default_factory=list)
    payloads: dict[tuple[int, int, int], bytes] = field(default_factory=dict)
    fail_on: set[tuple[int, IdRange]] = field(default_factory=set)

    def newest_message_id(self, chat_id: int) -> int | None:
        msgs = self.messages.get(chat_id, [])
        return max((m.message_id for m in msgs), default=None)

    def fetch_range(self, chat_id: int, window: IdRange) -> Iterable[IncomingMessage]:
        self.requested.append((chat_id, window))
        if (chat_id, window) in self.fail_on:
            raise RuntimeError(f"scripted failure on {chat_id} {window}")
        return sorted(
            (m for m in self.messages.get(chat_id, []) if window.contains(m.message_id)),
            key=lambda m: m.message_id,
        )

    def download_media(
        self, chat_id: int, message_id: int, index: int, destination: Path
    ) -> int:
        payload = self.payloads.get((chat_id, message_id, index), b"")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return len(payload)


class RateLimited(Exception):
    """The server told us to wait.

    Declared here, not in `ingest.py`, so `backfill` can handle it without
    importing telethon — it is the one library exception the archiver's control
    flow genuinely depends on. `ingest.py` translates telethon's
    `FloodWaitError` into this.

    The wait is **not** advisory. Ignoring a stated flood wait escalates to a
    longer one and eventually to an account ban, which for an MTProto user
    session means losing read access to the signal channel entirely.
    """

    def __init__(self, retry_after_seconds: float, detail: str = "") -> None:
        super().__init__(f"rate limited for {retry_after_seconds:.0f}s: {detail}")
        self.retry_after_seconds = float(retry_after_seconds)
        self.detail = detail


class ChatUnavailable(Exception):
    """The chat cannot be read: not joined, banned, deleted, or made private.

    Distinct from `RateLimited` because the remedy is a human one. Retrying
    forever against a channel we have been removed from looks identical to a
    quiet archiver, which is how a week of missed signals goes unnoticed.
    """
