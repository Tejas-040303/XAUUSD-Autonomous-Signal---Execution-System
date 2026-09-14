"""The only module that imports telethon.

`tests/test_architecture.py` enforces that with an `ast` scan, so an aliased
import elsewhere cannot slip past. The boundary matters for the same reason it
matters around `MetaTrader5`: everything upstream of here works on plain
dataclasses from `model.py`, which is what makes the archiver's crash-safety
logic testable without an MTProto client.

Why a user account and not a bot
--------------------------------
A bot cannot do this job at all. A bot only sees chats it has been added to, you
cannot add your bot to someone else's VIP channel, and the Bot API exposes no
method to fetch past messages — which spec §28's history backfill requires. So
reading is MTProto with `api_id`/`api_hash`, and the `.session` file that
produces is a full-account credential that bypasses the account password and
2FA. Writing status messages is a separate Bot API token, so a leak there is
bounded to "someone can post in your status chat".

Everything here is read-only. This module never sends a message, never joins
anything, never marks anything read. Status output goes through the outbox and a
different credential entirely.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from xauusd.archive import fetcher, store
from xauusd.archive.media import ALLOWED_MIME, DeclaredMedia, MediaKind, MediaLimits
from xauusd.archive.ranges import IdRange, to_telethon_bounds
from xauusd.clock import Clock
from xauusd.telegram.model import (
    ChatUnavailable,
    Deletion,
    IncomingMessage,
    RateLimited,
)

# Telegram re-encodes every uploaded photo to JPEG, so a `MessageMediaPhoto`
# carries no MIME field of its own and one is supplied here. Documents declare
# their own type and are taken at their word (then checked against the
# allowlist, and against the bytes after download).
_PHOTO_MIME = "image/jpeg"

# One media per Telegram message: an "album" is several messages sharing a
# `grouped_id`, each with a single attachment. The `idx` column and the tuple in
# `IncomingMessage.media` exist so that stops being an assumption baked into the
# schema, and so album members remain linkable through `grouped_id`.
_ONLY_INDEX = 0


def _import_telethon() -> tuple[Any, Any, Any, Any]:
    """Import telethon on demand, with an actionable message if it is absent.

    Deferred rather than top-level so every other module stays importable
    without the optional extra — `tests/test_architecture.py` imports the whole
    package, and the `ast` lint still sees these names regardless of where the
    import statement sits.
    """
    try:
        from telethon import TelegramClient, errors, events, utils
        from telethon.tl import types
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "telethon is required for Telegram ingestion but is not installed. "
            "Install the extra: python3 -m pip install -e '.[telegram]'"
        ) from exc
    return TelegramClient, types, errors, (events, utils)


# -- conversion ---------------------------------------------------------------
# Pure functions over telethon objects. Testable by constructing `types.Message`
# directly, with no client and no network.


def _peer_id(peer: Any, utils: Any) -> int | None:
    """Marked peer id, or `None`.

    `get_peer_id` returns the canonical marked form — `-100…` for a channel,
    negative for a legacy group, positive for a user — which is what config
    pins and what `allowed_sender_ids` is compared against. Using the unmarked
    id would make a user id and a chat id collide.
    """
    return None if peer is None else int(utils.get_peer_id(peer))


def _largest_photo_size(photo: Any, types: Any) -> tuple[int | None, int | None, int | None]:
    """`(width, height, bytes)` of the largest available rendition.

    Telegram offers several sizes; the largest is the one telethon downloads by
    default, so it is the one whose limits must be checked. `PhotoStrippedSize`
    and `PhotoSizeEmpty` carry no dimensions and are skipped —
    `PhotoStrippedSize` is the few-byte blur placeholder, never a real image.
    """
    best: tuple[int | None, int | None, int | None] = (None, None, None)
    best_pixels = -1
    for size in getattr(photo, "sizes", None) or []:
        width = getattr(size, "w", None)
        height = getattr(size, "h", None)
        if width is None or height is None:
            continue
        if isinstance(size, types.PhotoSizeProgressive):
            # A progressive JPEG declares a list of prefix lengths; the full
            # image is the largest of them.
            byte_size = max(getattr(size, "sizes", None) or [0]) or None
        else:
            byte_size = getattr(size, "size", None)
        pixels = width * height
        if pixels > best_pixels:
            best_pixels = pixels
            best = (width, height, byte_size)
    return best


def _declared_media(message: Any, types: Any) -> tuple[DeclaredMedia, ...]:
    """Describe the attachment, if there is one we would ever want.

    Link-preview images (`MessageMediaWebPage`) are ignored on purpose: they are
    generated by Telegram from a URL in the text, not posted by the provider, so
    treating one as a signal screenshot would let any link in the channel inject
    an image into the corpus.
    """
    media = getattr(message, "media", None)
    if media is None:
        return ()

    if isinstance(media, types.MessageMediaPhoto):
        photo = getattr(media, "photo", None)
        if photo is None or isinstance(photo, types.PhotoEmpty):
            return ()
        width, height, byte_size = _largest_photo_size(photo, types)
        return (
            DeclaredMedia(
                kind=MediaKind.PHOTO,
                mime=_PHOTO_MIME,
                size_bytes=byte_size,
                width=width,
                height=height,
                name=None,
            ),
        )

    if isinstance(media, types.MessageMediaDocument):
        document = getattr(media, "document", None)
        if document is None or isinstance(document, types.DocumentEmpty):
            return ()
        width = height = None
        name = None
        for attribute in getattr(document, "attributes", None) or []:
            if isinstance(attribute, types.DocumentAttributeImageSize):
                width, height = attribute.w, attribute.h
            elif isinstance(attribute, types.DocumentAttributeFilename):
                # Sender-controlled. Stored as data; never used to build a path.
                name = attribute.file_name
        return (
            DeclaredMedia(
                kind=MediaKind.DOCUMENT,
                mime=getattr(document, "mime_type", None),
                size_bytes=getattr(document, "size", None),
                width=width,
                height=height,
                name=name,
            ),
        )

    # Polls, contacts, locations, dice, stickers, venues: not a signal and not a
    # screenshot. Recorded as a message with no media rather than dropped, so
    # the id is covered and coverage stays honest.
    return ()


def to_incoming(message: Any, chat_id: int, types: Any, utils: Any) -> IncomingMessage | None:
    """Convert one telethon message. `None` for a placeholder to skip.

    `MessageEmpty` is Telegram's stand-in for a message that has been deleted or
    is inaccessible. It carries no content, so there is nothing to archive — but
    the surrounding range is still marked scanned, which is exactly the
    distinction `scan_ranges` exists to keep: scanned-and-absent is not the same
    as never-looked-at.
    """
    if isinstance(message, types.MessageEmpty):
        return None

    # MessageService subclasses Message in telethon, so this test must come
    # first or every join and pin would be archived as a normal message.
    is_service = isinstance(message, types.MessageService)

    reply_to = getattr(message, "reply_to", None)
    return IncomingMessage(
        chat_id=chat_id,
        message_id=int(message.id),
        posted_at=message.date,
        text=getattr(message, "message", None) or "",
        sender_id=_peer_id(getattr(message, "from_id", None), utils),
        edited_at=getattr(message, "edit_date", None),
        reply_to_id=getattr(reply_to, "reply_to_msg_id", None),
        grouped_id=getattr(message, "grouped_id", None),
        is_outgoing=bool(getattr(message, "out", False)),
        is_forwarded=getattr(message, "fwd_from", None) is not None,
        is_service=is_service,
        media=() if is_service else _declared_media(message, types),
    )


# -- reader -------------------------------------------------------------------


class TelethonReader:
    """`TelegramReader` over a connected telethon client.

    Synchronous by design. The archiver's work against one chat is serialised by
    the server's own rate limiting anyway, so concurrency here would buy nothing
    and cost the testability the Protocol provides.
    """

    def __init__(self, client: Any, clock: Clock) -> None:
        self._client = client
        self._clock = clock
        _, self._types, self._errors, (_, self._utils) = _import_telethon()

    def _translate(self, exc: Exception) -> Exception:
        """Map telethon errors onto the two the archiver can act on."""
        errors = self._errors
        if isinstance(exc, errors.FloodWaitError):
            # `seconds` comes from the RPC error rather than the constructor,
            # so it is read defensively; a flood wait with no stated duration
            # still has to back off by something.
            return RateLimited(float(getattr(exc, "seconds", 60) or 60), str(exc))
        if isinstance(
            exc,
            (
                errors.ChannelPrivateError,
                errors.ChatIdInvalidError,
                errors.ChannelInvalidError,
            ),
        ):
            return ChatUnavailable(str(exc))
        return exc

    def newest_message_id(self, chat_id: int) -> int | None:
        try:
            found = self._client.get_messages(chat_id, limit=1)
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        for message in found or []:
            identifier = getattr(message, "id", None)
            if identifier:
                return int(identifier)
        return None

    def fetch_range(self, chat_id: int, window: IdRange) -> Iterator[IncomingMessage]:
        min_id, max_id = to_telethon_bounds(window)
        try:
            # `reverse=True` yields oldest first, which is the order the archive
            # wants: a partially consumed batch then leaves a prefix rather than
            # a hole in the middle.
            iterator = self._client.iter_messages(
                chat_id, min_id=min_id, max_id=max_id, reverse=True
            )
            for message in iterator:
                converted = to_incoming(message, chat_id, self._types, self._utils)
                if converted is not None:
                    yield converted
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc

    def download_media(
        self, chat_id: int, message_id: int, index: int, destination: Path
    ) -> int:
        """Re-fetch the message, then download its attachment.

        Re-fetching rather than holding the object is deliberate: a
        `file_reference` expires after roughly a day, and a retry against a
        stale one fails with `FileReferenceExpiredError`. Fetching by id always
        yields a fresh reference, which makes the retry path work without a
        special case.
        """
        if index != _ONLY_INDEX:
            raise ValueError(
                f"a Telegram message carries at most one attachment; got index {index}. "
                "Album members are separate messages sharing a grouped_id."
            )
        try:
            message = self._client.get_messages(chat_id, ids=message_id)
            if message is None:
                raise FileNotFoundError(f"message {chat_id}/{message_id} is gone")
            self._client.download_media(message, file=str(destination))
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc
        return destination.stat().st_size if destination.exists() else 0


# -- live listener ------------------------------------------------------------


class LiveArchiver:
    """Archive messages as they arrive, plus edits and deletions.

    Handlers do the database write inline — a few microseconds for a SQLite
    commit — and leave media `pending`. A separate worker coroutine drains those
    with `await`, because a download takes seconds and blocking telethon's event
    loop for that long stalls the connection that is supposed to be receiving
    the next signal.
    """

    def __init__(
        self,
        client: Any,
        archive_conn: sqlite3.Connection,
        *,
        chat_ids: Iterable[int],
        clock: Clock,
        limits: MediaLimits,
        media_root: Path,
        media_poll_seconds: float = 2.0,
        on_message: Callable[[IncomingMessage, store.RecordResult], None] | None = None,
        on_error: Callable[[str, BaseException], None] | None = None,
    ) -> None:
        self._client = client
        self._conn = archive_conn
        self._chat_ids = list(chat_ids)
        self._clock = clock
        self._limits = limits
        self._media_root = media_root
        self._poll = media_poll_seconds
        self._on_message = on_message
        self._on_error = on_error
        _, self._types, self._errors, (self._events, self._utils) = _import_telethon()

    def register(self) -> None:
        """Attach handlers. Call before `run_until_disconnected`."""
        events = self._events

        @self._client.on(events.NewMessage(chats=self._chat_ids))
        async def _new(event: Any) -> None:  # pragma: no cover - needs a live client
            await self._handle(event.message, "NewMessage")

        @self._client.on(events.MessageEdited(chats=self._chat_ids))
        async def _edited(event: Any) -> None:  # pragma: no cover
            # An edit is a revision, never an overwrite: the text we may already
            # have traded on stays provable.
            await self._handle(event.message, "MessageEdited")

        @self._client.on(events.MessageDeleted(chats=self._chat_ids))
        async def _deleted(event: Any) -> None:  # pragma: no cover
            try:
                chat_id = event.chat_id
                if chat_id is None:
                    # Telegram omits the chat for deletions in some contexts.
                    # Nothing can be attributed, so it is dropped rather than
                    # guessed onto a chat.
                    return
                for message_id in event.deleted_ids:
                    store.record_deletion(
                        self._conn,
                        Deletion(
                            chat_id=int(chat_id),
                            message_id=int(message_id),
                            noticed_at=self._clock.now_utc(),
                        ),
                    )
            except Exception as exc:  # noqa: BLE001
                self._report("MessageDeleted", exc)

    async def _handle(self, message: Any, source: str) -> None:  # pragma: no cover
        try:
            chat_id = int(self._utils.get_peer_id(message.peer_id))
            converted = to_incoming(message, chat_id, self._types, self._utils)
            if converted is None:
                return
            result = store.record_message(
                self._conn, converted, self._clock.now_utc(), self._limits
            )
            if self._on_message is not None:
                self._on_message(converted, result)
        except Exception as exc:  # noqa: BLE001
            # A handler that raises kills telethon's dispatch loop for every
            # subsequent event, so one malformed message must not take the
            # listener down with it.
            self._report(source, exc)

    async def drain_media(self, stop: asyncio.Event) -> None:  # pragma: no cover
        """Download attachments recorded as pending, until `stop` is set."""
        while not stop.is_set():
            try:
                await self._drain_once()
            except RateLimited as exc:
                self._report("drain_media", exc)
                await asyncio.sleep(exc.retry_after_seconds)
                continue
            except Exception as exc:  # noqa: BLE001
                self._report("drain_media", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll)
            except asyncio.TimeoutError:
                pass

    async def _drain_once(self) -> int:  # pragma: no cover
        from pathlib import PurePosixPath

        rows = self._conn.execute(
            """
            SELECT chat_id, message_id, idx, rel_path, kind, declared_mime, declared_bytes,
                   declared_w, declared_h, declared_name, attempts
              FROM media
             WHERE state IN ('pending', 'failed') AND rel_path IS NOT NULL
             ORDER BY message_id DESC, idx
             LIMIT 20
            """
        ).fetchall()

        stored = 0
        for row in rows:
            task = store.DownloadTask(
                chat_id=int(row["chat_id"]),
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
            destination = fetcher.destination_for(self._media_root, task)
            try:
                message = await self._client.get_messages(
                    task.chat_id, ids=task.message_id
                )
                if message is None:
                    store.fail_download(
                        self._conn, task, "message no longer retrievable", permanent=True
                    )
                    continue
                await self._client.download_media(message, file=str(destination))
            except self._errors.FloodWaitError as exc:
                raise RateLimited(float(getattr(exc, "seconds", 60) or 60), str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                fetcher.record_failure(self._conn, task, exc, int(row["attempts"]))
                continue
            if fetcher.record_downloaded(
                self._conn, task, destination, limits=self._limits, clock=self._clock
            ):
                stored += 1
        return stored

    def _report(self, source: str, exc: BaseException) -> None:
        if self._on_error is not None:
            self._on_error(source, exc)


def build_client(
    api_id: int, api_hash: str, session_path: str | Path, *, clock: Clock | None = None
) -> Any:
    """Construct a telethon client. Does not connect.

    The session file it will create is a full-account credential: it grants API
    access to the account, bypasses the account password and 2FA, and is revoked
    only by terminating the session from another device. `.gitignore` covers
    `*.session`, but a backup or a container image will happily carry one out.
    """
    _ = clock
    TelegramClient, _types, _errors, _ = _import_telethon()
    return TelegramClient(str(session_path), api_id, api_hash)


def utc_now_from(message: Any) -> datetime:
    """A message's own timestamp. Telethon returns UTC-aware datetimes."""
    return message.date
