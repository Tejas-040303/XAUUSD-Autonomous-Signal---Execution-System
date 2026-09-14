"""Backfill: gap-free coverage, and the ordering that makes a crash survivable.

The rule under test: **messages are recorded before their range is marked
scanned.** The two are separate transactions, so a crash between them re-scans a
window already archived — harmless, because every archive write is idempotent.
The opposite order would mark coverage for messages that were never stored, and
nothing would ever look at that range again.

One direction costs duplicate work. The other loses signals permanently and
silently. These tests assert the cheap direction.
"""

from __future__ import annotations

import datetime as dt

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from xauusd.archive.media import DeclaredMedia, MediaKind
from xauusd.archive.ranges import IdRange, covers, gaps
from xauusd.archive.store import load_scan_ranges
from xauusd.telegram.backfill import plan, retry_unresolved_media, run
from xauusd.telegram.model import (
    ChatUnavailable,
    FakeReader,
    IncomingMessage,
    RateLimited,
)

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 9, 14, 5, 45, tzinfo=UTC)
CHAT = -1001234567890

PHOTO = DeclaredMedia(
    kind=MediaKind.PHOTO, mime="image/jpeg", size_bytes=50_000, width=1280, height=720
)


def history(ids, *, with_media=()) -> FakeReader:
    """A chat whose existing message ids are exactly `ids` — deliberately
    non-contiguous in most tests, because Telegram ids are."""
    messages = [
        IncomingMessage(
            chat_id=CHAT,
            message_id=i,
            posted_at=T0 + dt.timedelta(minutes=i),
            text=f"signal {i}",
            media=(PHOTO,) if i in with_media else (),
        )
        for i in ids
    ]
    reader = FakeReader(messages={CHAT: messages})
    for i in with_media:
        reader.payloads[(CHAT, i, 0)] = b"\xff\xd8\xff" + b"x" * 49_000
    return reader


def do_run(reader, conn, clock, limits, media_root, **kwargs):
    return run(
        reader,
        conn,
        CHAT,
        clock=clock,
        limits=limits,
        media_root=media_root,
        **kwargs,
    )


# -- planning ----------------------------------------------------------------


def test_a_fresh_chat_plans_the_whole_history():
    assert plan([], IdRange(1, 450), batch_size=200) == (
        IdRange(401, 450),
        IdRange(201, 400),
        IdRange(1, 200),
    )


def test_newest_batches_come_first():
    """An interrupted first backfill should leave the MOST RECENT history
    present: a corpus of two-year-old layouts trains the parser on a format the
    provider has since changed."""
    batches = plan([], IdRange(1, 450), batch_size=200)
    assert batches[0].hi == 450


def test_only_the_holes_are_planned():
    assert plan([IdRange(1, 100), IdRange(201, 450)], IdRange(1, 450), batch_size=50) == (
        IdRange(151, 200),
        IdRange(101, 150),
    )


def test_a_covered_chat_plans_nothing():
    assert plan([IdRange(1, 450)], IdRange(1, 450)) == ()


def test_batch_size_must_be_positive():
    with pytest.raises(ValueError, match="must be positive"):
        plan([], IdRange(1, 10), batch_size=0)


# -- coverage ----------------------------------------------------------------


def test_a_full_run_leaves_no_gaps(archive_db, limits, frozen_clock, media_root):
    reader = history([1, 2, 5, 9, 40, 41, 99])
    report = do_run(reader, archive_db, frozen_clock, limits, media_root, batch_size=10)
    assert report.messages_new == 7
    assert report.remaining_gaps == ()
    assert report.complete


def test_sparse_ids_are_covered_not_reported_as_gaps(archive_db, limits, frozen_clock, media_root):
    """The distinction the whole design turns on: scanned-and-absent is not the
    same as never-looked-at. Ids 3, 4, 6, 7, 8 do not exist, and a gap detector
    built on missing ids would raise a permanent, unfixable alarm."""
    reader = history([1, 2, 5, 9])
    do_run(reader, archive_db, frozen_clock, limits, media_root, batch_size=10)
    assert covers(load_scan_ranges(archive_db, CHAT), IdRange(1, 9))
    assert archive_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4


def test_an_empty_chat_records_no_coverage(archive_db, limits, frozen_clock, media_root):
    """A scan range here would later claim coverage of ids that do not exist
    yet and suppress the channel's first real message."""
    report = do_run(FakeReader(), archive_db, frozen_clock, limits, media_root)
    assert report.messages_seen == 0
    assert load_scan_ranges(archive_db, CHAT) == ()


def test_a_second_run_re_fetches_nothing(archive_db, limits, frozen_clock, media_root):
    reader = history([1, 2, 5, 9])
    do_run(reader, archive_db, frozen_clock, limits, media_root, batch_size=10)
    before = len(reader.requested)
    report = do_run(reader, archive_db, frozen_clock, limits, media_root, batch_size=10)
    assert report.windows_planned == 0
    assert len(reader.requested) == before


def test_new_messages_since_the_last_run_are_picked_up(
    archive_db, limits, frozen_clock, media_root
):
    reader = history([1, 2, 5])
    do_run(reader, archive_db, frozen_clock, limits, media_root, batch_size=10)
    reader.messages[CHAT].append(
        IncomingMessage(chat_id=CHAT, message_id=12, posted_at=T0, text="later signal")
    )
    report = do_run(reader, archive_db, frozen_clock, limits, media_root, batch_size=10)
    assert report.messages_new == 1


def test_oldest_message_id_bounds_the_walk(archive_db, limits, frozen_clock, media_root):
    reader = history(list(range(1, 60)))
    report = do_run(
        reader, archive_db, frozen_clock, limits, media_root, oldest_message_id=50, batch_size=10
    )
    assert report.messages_new == 10
    assert all(w.lo >= 50 for _, w in reader.requested)


def test_max_windows_bounds_one_invocation(archive_db, limits, frozen_clock, media_root):
    """So a first run against years of history can be done in sessions."""
    reader = history(list(range(1, 100)))
    report = do_run(
        reader, archive_db, frozen_clock, limits, media_root, batch_size=10, max_windows=3
    )
    assert report.windows_scanned == 3
    assert report.remaining_gaps != ()
    assert not report.complete


def test_fetches_use_inclusive_windows(archive_db, limits, frozen_clock, media_root):
    reader = history([1, 50])
    do_run(reader, archive_db, frozen_clock, limits, media_root, batch_size=25)
    for _, window in reader.requested:
        assert window.lo >= 1 and window.hi <= 50


# -- crash safety ------------------------------------------------------------


def test_a_failed_batch_is_not_marked_scanned(archive_db, limits, frozen_clock, media_root):
    """The ordering rule. If the range were marked first, these messages would
    be lost permanently because nothing would look at that range again."""
    reader = history(list(range(1, 41)))
    reader.fail_on.add((CHAT, IdRange(1, 20)))
    report = do_run(reader, archive_db, frozen_clock, limits, media_root, batch_size=20)

    assert report.stopped_early is not None
    scanned = load_scan_ranges(archive_db, CHAT)
    assert IdRange(1, 20) in gaps(scanned, IdRange(1, 40))
    assert covers(scanned, IdRange(21, 40)), "the batch that succeeded stays recorded"


def test_the_failed_batch_is_retried_on_the_next_run(
    archive_db, limits, frozen_clock, media_root
):
    reader = history(list(range(1, 41)))
    reader.fail_on.add((CHAT, IdRange(1, 20)))
    do_run(reader, archive_db, frozen_clock, limits, media_root, batch_size=20)

    reader.fail_on.clear()
    report = do_run(reader, archive_db, frozen_clock, limits, media_root, batch_size=20)
    assert report.messages_new == 20
    assert report.remaining_gaps == ()


def test_a_rate_limit_stops_the_run_and_says_how_long(
    archive_db, limits, frozen_clock, media_root
):
    """Ignoring a stated flood wait escalates to a longer one and eventually to
    an account ban — which for an MTProto session means losing read access to
    the channel entirely."""

    class Limited(FakeReader):
        def fetch_range(self, chat_id, window):
            raise RateLimited(300.0, "FLOOD_WAIT_300")

    reader = Limited(messages={CHAT: history([1, 2, 3]).messages[CHAT]})
    report = do_run(reader, archive_db, frozen_clock, limits, media_root)
    assert "300s" in report.stopped_early
    assert load_scan_ranges(archive_db, CHAT) == ()


def test_an_unavailable_chat_stops_the_run(archive_db, limits, frozen_clock, media_root):
    """Distinct from a rate limit because the remedy is a human one. Retrying
    forever against a channel we were removed from looks identical to a quiet
    archiver, which is how a week of missed signals goes unnoticed."""

    class Gone(FakeReader):
        def newest_message_id(self, chat_id):
            raise ChatUnavailable("CHANNEL_PRIVATE")

    report = do_run(Gone(), archive_db, frozen_clock, limits, media_root)
    assert "chat unavailable" in report.stopped_early


def test_messages_survive_when_media_fetching_fails(
    archive_db, limits, frozen_clock, media_root
):
    """Media is fetched after the message rows are committed, so a download
    failure leaves a recorded message with a retryable attachment rather than an
    unrecorded message."""

    class NoMedia(FakeReader):
        def download_media(self, chat_id, message_id, index, destination):
            raise OSError("connection reset by peer")

    reader = NoMedia(messages={CHAT: history([7], with_media=(7,)).messages[CHAT]})
    report = do_run(reader, archive_db, frozen_clock, limits, media_root, batch_size=10)

    assert report.messages_new == 1
    assert report.media_unresolved == 1
    stored = archive_db.execute("SELECT content_state FROM messages").fetchone()
    assert stored["content_state"] == "pending_media"
    # The range is still marked: the attachment is tracked per-row and retried
    # independently, rather than by re-fetching the whole batch to find it.
    assert covers(load_scan_ranges(archive_db, CHAT), IdRange(7, 7))


# -- media -------------------------------------------------------------------


def test_media_is_downloaded_and_fingerprinted(archive_db, limits, frozen_clock, media_root):
    reader = history([7], with_media=(7,))
    report = do_run(reader, archive_db, frozen_clock, limits, media_root, batch_size=10)

    assert report.media_stored == 1
    stored = archive_db.execute(
        "SELECT state, sha256, stored_bytes, rel_path FROM media"
    ).fetchone()
    assert stored["state"] == "stored"
    assert len(stored["sha256"]) == 64
    assert (media_root / stored["rel_path"]).is_file()
    assert archive_db.execute("SELECT content_hash FROM messages").fetchone()[0] is not None


def test_unresolved_media_is_recovered_by_a_retry_pass(
    archive_db, limits, frozen_clock, media_root
):
    """Separate from the walk because the two fail independently: re-scanning a
    whole range to recover one screenshot would re-read hundreds of messages."""
    calls = {"n": 0}

    class FlakyOnce(FakeReader):
        def download_media(self, chat_id, message_id, index, destination):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("transient DC error")
            return super().download_media(chat_id, message_id, index, destination)

    reader = FlakyOnce(messages={CHAT: history([7], with_media=(7,)).messages[CHAT]})
    reader.payloads[(CHAT, 7, 0)] = b"\xff\xd8\xff" + b"x" * 49_000

    report = do_run(reader, archive_db, frozen_clock, limits, media_root, batch_size=10)
    assert report.media_unresolved == 1

    recovered = retry_unresolved_media(
        reader, archive_db, CHAT, clock=frozen_clock, limits=limits, media_root=media_root
    )
    assert recovered == 1
    assert archive_db.execute("SELECT content_hash FROM messages").fetchone()[0] is not None


def test_a_bomb_is_never_downloaded(archive_db, limits, frozen_clock, media_root):
    bomb = DeclaredMedia(
        kind=MediaKind.PHOTO, mime="image/jpeg", size_bytes=400_000, width=30_000, height=30_000
    )
    reader = FakeReader(
        messages={
            CHAT: [
                IncomingMessage(
                    chat_id=CHAT, message_id=7, posted_at=T0, text="", media=(bomb,)
                )
            ]
        }
    )
    report = do_run(reader, archive_db, frozen_clock, limits, media_root, batch_size=10)

    assert report.media_stored == 0
    assert list(media_root.rglob("*")) == [], "no bytes were fetched"
    stored = archive_db.execute("SELECT state, reject_reason FROM media").fetchone()
    assert stored["reject_reason"] == "DECLARED_TOO_MANY_PIXELS"


# -- properties --------------------------------------------------------------


@pytest.mark.property
@settings(max_examples=40, deadline=None)
@given(
    ids=st.lists(st.integers(1, 120), min_size=1, max_size=25, unique=True),
    batch=st.integers(1, 40),
)
def test_any_history_is_fully_covered_by_one_run(ids, batch, tmp_path_factory):
    """Whatever the id distribution and batch size, one completed run leaves no
    gap below the newest message — and archives every message exactly once."""
    from xauusd.archive.media import MediaLimits
    from xauusd.clock import FakeClock
    from xauusd.db.migrate import migrate
    from xauusd.db.store import Role, connect

    root = tmp_path_factory.mktemp("prop")
    conn = connect(root / "a.db", Role.ARCHIVE)
    migrate(conn, Role.ARCHIVE)
    try:
        reader = history(sorted(ids))
        report = run(
            reader,
            conn,
            CHAT,
            clock=FakeClock(at=T0),
            limits=MediaLimits(max_bytes=1 << 23, max_pixels=40_000_000),
            media_root=root,
            batch_size=batch,
        )
        assert report.remaining_gaps == ()
        assert covers(load_scan_ranges(conn, CHAT), IdRange(1, max(ids)))
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == len(set(ids))
        assert report.messages_new == len(set(ids))
    finally:
        conn.close()


@pytest.mark.property
@settings(max_examples=40, deadline=None)
@given(
    ids=st.lists(st.integers(1, 80), min_size=1, max_size=20, unique=True),
    batch=st.integers(1, 20),
    runs=st.integers(2, 4),
)
def test_repeated_runs_are_idempotent(ids, batch, runs, tmp_path_factory):
    """Overlapping backfills are the normal case, not an error path."""
    from xauusd.archive.media import MediaLimits
    from xauusd.clock import FakeClock
    from xauusd.db.migrate import migrate
    from xauusd.db.store import Role, connect

    root = tmp_path_factory.mktemp("idem")
    conn = connect(root / "a.db", Role.ARCHIVE)
    migrate(conn, Role.ARCHIVE)
    try:
        reader = history(sorted(ids))
        total_new = 0
        for _ in range(runs):
            report = run(
                reader,
                conn,
                CHAT,
                clock=FakeClock(at=T0),
                limits=MediaLimits(max_bytes=1 << 23, max_pixels=40_000_000),
                media_root=root,
                batch_size=batch,
            )
            total_new += report.messages_new
        assert total_new == len(set(ids)), "a message must be inserted exactly once"
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == len(set(ids))
    finally:
        conn.close()
