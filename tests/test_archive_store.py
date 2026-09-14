"""Archive writes: idempotence, edit history, and the fail-closed fingerprint.

The archiver re-sees messages constantly — backfill windows overlap on purpose,
a restart re-scans the newest block, and the live listener can race a catch-up
pass — so every write here has to be safe to repeat. These tests call each one
twice on purpose.
"""

from __future__ import annotations

import datetime as dt

import pytest

from xauusd.archive.media import DeclaredMedia, MediaKind
from xauusd.archive.store import (
    COMPLETE,
    MEDIA_FAILED,
    PENDING_MEDIA,
    add_scan_range,
    find_duplicates,
    load_scan_ranges,
    meta_get,
    meta_set,
    record_deletion,
    record_message,
)
from xauusd.archive.ranges import IdRange
from xauusd.telegram.model import Deletion, IncomingMessage

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 9, 14, 5, 45, tzinfo=UTC)
CHAT = -1001234567890

PHOTO = DeclaredMedia(
    kind=MediaKind.PHOTO, mime="image/jpeg", size_bytes=50_000, width=1280, height=720
)
PDF = DeclaredMedia(
    kind=MediaKind.DOCUMENT,
    mime="application/pdf",
    size_bytes=1000,
    width=None,
    height=None,
    name="../../.env",
)


def msg(message_id: int = 10, **overrides) -> IncomingMessage:
    base = dict(
        chat_id=CHAT,
        message_id=message_id,
        posted_at=T0,
        text="XAUUSD BUY 2650.50 SL 2645 TP1 2655",
    )
    base.update(overrides)
    return IncomingMessage(**base)


def row(conn, message_id: int = 10):
    return conn.execute(
        "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?", (CHAT, message_id)
    ).fetchone()


# -- idempotence -------------------------------------------------------------


def test_a_text_message_is_complete_immediately(archive_db, limits):
    result = record_message(archive_db, msg(), T0, limits)
    assert result.inserted
    assert not result.needs_download
    stored = row(archive_db)
    assert stored["content_state"] == COMPLETE
    assert stored["content_hash"] is not None


def test_re_archiving_is_a_no_op(archive_db, limits):
    record_message(archive_db, msg(), T0, limits)
    again = record_message(archive_db, msg(), T0 + dt.timedelta(hours=1), limits)
    assert not again.inserted
    assert archive_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_first_seen_at_is_not_overwritten_on_a_re_see(archive_db, limits):
    """Ingestion lag is reconstructed from `posted_at` vs `first_seen_at`, and a
    re-see hours later must not make the original look late."""
    record_message(archive_db, msg(), T0, limits)
    first = row(archive_db)["first_seen_at"]
    record_message(archive_db, msg(), T0 + dt.timedelta(days=2), limits)
    assert row(archive_db)["first_seen_at"] == first


def test_re_seeing_a_message_does_not_re_queue_stored_media(archive_db, limits):
    """Otherwise every overlapping backfill window would re-download the whole
    corpus."""
    result = record_message(archive_db, msg(11, text="", media=(PHOTO,)), T0, limits)
    from xauusd.archive.store import complete_download

    complete_download(archive_db, result.downloads[0], "c" * 64, 49_000, T0)
    again = record_message(archive_db, msg(11, text="", media=(PHOTO,)), T0, limits)
    assert again.downloads == ()


# -- the fail-closed fingerprint ---------------------------------------------


def test_fingerprint_is_null_while_media_is_unresolved(archive_db, limits):
    """A message whose identity cannot be computed must not be judged a
    duplicate of anything."""
    record_message(archive_db, msg(11, text="", media=(PHOTO,)), T0, limits)
    stored = row(archive_db, 11)
    assert stored["content_state"] == PENDING_MEDIA
    assert stored["content_hash"] is None
    assert stored["hash_version"] is None


def test_fingerprint_appears_once_media_is_stored(archive_db, limits):
    from xauusd.archive.store import complete_download

    result = record_message(archive_db, msg(11, text="", media=(PHOTO,)), T0, limits)
    complete_download(archive_db, result.downloads[0], "c" * 64, 49_000, T0)
    stored = row(archive_db, 11)
    assert stored["content_state"] == COMPLETE
    assert stored["content_hash"] is not None


def test_a_policy_rejected_attachment_does_not_block_the_fingerprint(archive_db, limits):
    """A rejection is known content: we can say exactly what it was and why.
    So a text signal carrying a stray PDF stays comparable, rather than becoming
    permanently untradeable."""
    record_message(archive_db, msg(12, media=(PDF,)), T0, limits)
    stored = row(archive_db, 12)
    assert stored["content_state"] == MEDIA_FAILED
    assert stored["content_hash"] is not None


def test_the_rejection_reason_is_structured(archive_db, limits):
    """CLAUDE.md: never a bare "rejected"."""
    record_message(archive_db, msg(12, media=(PDF,)), T0, limits)
    media = archive_db.execute(
        "SELECT state, reject_reason, declared_name FROM media WHERE message_id = 12"
    ).fetchone()
    assert media["state"] == "rejected"
    assert media["reject_reason"] == "MIME_NOT_ALLOWED"
    # The hostile name is kept as evidence but never became a path.
    assert media["declared_name"] == "../../.env"


def test_a_failed_download_keeps_the_fingerprint_null(archive_db, limits):
    from xauusd.archive.store import fail_download

    result = record_message(archive_db, msg(11, text="", media=(PHOTO,)), T0, limits)
    fail_download(archive_db, result.downloads[0], "connection reset", permanent=False)
    stored = row(archive_db, 11)
    assert stored["content_state"] == PENDING_MEDIA
    assert stored["content_hash"] is None


def test_a_permanently_failed_download_becomes_comparable(archive_db, limits):
    """Past the attempt cap the content is known-unavailable rather than unknown,
    so the message stops being stuck outside the dedupe system forever."""
    from xauusd.archive.store import fail_download

    result = record_message(archive_db, msg(11, text="x", media=(PHOTO,)), T0, limits)
    fail_download(archive_db, result.downloads[0], "gone", permanent=True)
    stored = row(archive_db, 11)
    assert stored["content_state"] == MEDIA_FAILED
    assert stored["content_hash"] is not None


def test_bytes_contradicting_the_declaration_are_rejected(archive_db, limits):
    from xauusd.archive.store import reject_download

    result = record_message(archive_db, msg(11, text="", media=(PHOTO,)), T0, limits)
    reject_download(archive_db, result.downloads[0], "SIZE_MISMATCH", "declared 50000 stored 10")
    media = archive_db.execute(
        "SELECT state, rel_path, sha256 FROM media WHERE message_id = 11"
    ).fetchone()
    assert media["state"] == "rejected"
    # The path is cleared so nothing downstream reads a file we disowned.
    assert media["rel_path"] is None
    assert media["sha256"] is None


# -- edits -------------------------------------------------------------------


def test_an_edit_is_a_revision_not_an_overwrite(archive_db, limits):
    """A provider changing "SL 2645" to "SL 2635" after we entered is real. The
    SL we actually traded has to stay provable."""
    record_message(archive_db, msg(), T0, limits)
    edited = msg(text="XAUUSD BUY 2650.50 SL 2635 TP1 2655", edited_at=T0 + dt.timedelta(minutes=2))
    result = record_message(archive_db, edited, T0 + dt.timedelta(minutes=2), limits)

    assert result.edit_recorded
    assert "SL 2645" in row(archive_db)["text"], "the original must not be rewritten"
    assert row(archive_db)["edit_count"] == 1
    revision = archive_db.execute(
        "SELECT revision, text, content_hash FROM message_edits WHERE message_id = 10"
    ).fetchone()
    assert revision["revision"] == 1
    assert "SL 2635" in revision["text"]
    assert revision["content_hash"] is not None


def test_replaying_the_same_edit_adds_nothing(archive_db, limits):
    record_message(archive_db, msg(), T0, limits)
    edited = msg(text="changed", edited_at=T0 + dt.timedelta(minutes=2))
    assert record_message(archive_db, edited, T0, limits).edit_recorded
    assert not record_message(archive_db, edited, T0, limits).edit_recorded
    assert archive_db.execute("SELECT COUNT(*) FROM message_edits").fetchone()[0] == 1


def test_successive_edits_accumulate_revisions(archive_db, limits):
    record_message(archive_db, msg(), T0, limits)
    for minute in (2, 5, 9):
        record_message(
            archive_db,
            msg(text=f"v{minute}", edited_at=T0 + dt.timedelta(minutes=minute)),
            T0,
            limits,
        )
    revisions = archive_db.execute(
        "SELECT revision, text FROM message_edits WHERE message_id = 10 ORDER BY revision"
    ).fetchall()
    assert [r["revision"] for r in revisions] == [1, 2, 3]
    assert [r["text"] for r in revisions] == ["v2", "v5", "v9"]
    assert row(archive_db)["edit_count"] == 3


# -- deletions ---------------------------------------------------------------


def test_a_deletion_is_recorded_and_the_message_kept(archive_db, limits):
    """A provider deleting a losing call is exactly what the archive exists to
    remember."""
    record_message(archive_db, msg(), T0, limits)
    assert record_deletion(archive_db, Deletion(CHAT, 10, T0 + dt.timedelta(hours=1)))
    assert row(archive_db) is not None
    assert row(archive_db)["deleted_at"] is not None


def test_a_deletion_for_an_unknown_message_is_still_recorded(archive_db):
    """It means a gap in coverage, which is worth knowing."""
    assert not record_deletion(archive_db, Deletion(CHAT, 999, T0))
    noted = archive_db.execute(
        "SELECT was_known FROM message_deletions WHERE message_id = 999"
    ).fetchone()
    assert noted["was_known"] == 0


def test_deleted_at_records_the_first_notification(archive_db, limits):
    record_message(archive_db, msg(), T0, limits)
    record_deletion(archive_db, Deletion(CHAT, 10, T0 + dt.timedelta(hours=1)))
    first = row(archive_db)["deleted_at"]
    record_deletion(archive_db, Deletion(CHAT, 10, T0 + dt.timedelta(hours=5)))
    assert row(archive_db)["deleted_at"] == first


# -- dedupe window (spec §23) ------------------------------------------------


def test_a_repost_with_different_spacing_is_found_as_a_duplicate(archive_db, limits):
    record_message(archive_db, msg(10), T0, limits)
    record_message(
        archive_db,
        msg(11, text="xauusd   buy 2650.50   sl 2645 tp1 2655", posted_at=T0 + dt.timedelta(seconds=40)),
        T0,
        limits,
    )
    digest = row(archive_db)["content_hash"]
    found = find_duplicates(archive_db, CHAT, digest, 86_400, T0 + dt.timedelta(minutes=1))
    assert found == (10, 11)


def test_the_window_actually_bounds_the_search(archive_db, limits):
    """The regression guarded here: SQLite's own `datetime()` renders
    `2026-09-14 05:45:00` — space separator, no Z, no microseconds — which does
    not order correctly against the stored form. Comparing the two would give a
    window wrong by hours, silently."""
    record_message(archive_db, msg(10), T0, limits)
    digest = row(archive_db)["content_hash"]
    now = T0 + dt.timedelta(seconds=45)
    assert find_duplicates(archive_db, CHAT, digest, 86_400, now) == (10,)
    assert find_duplicates(archive_db, CHAT, digest, 10, now) == ()


def test_an_incomparable_message_is_never_a_duplicate(archive_db, limits):
    record_message(archive_db, msg(11, text="", media=(PHOTO,)), T0, limits)
    assert row(archive_db, 11)["content_hash"] is None
    # No hash means nothing to match: the NULL does the fail-closed work.
    assert find_duplicates(archive_db, CHAT, "c" * 64, 86_400, T0) == ()


def test_a_nonpositive_window_is_rejected(archive_db):
    with pytest.raises(ValueError, match="must be positive"):
        find_duplicates(archive_db, CHAT, "c" * 64, 0, T0)


def test_duplicates_do_not_cross_chats(archive_db, limits):
    record_message(archive_db, msg(10), T0, limits)
    record_message(archive_db, msg(10, chat_id=-100999), T0, limits)
    digest = row(archive_db)["content_hash"]
    assert find_duplicates(archive_db, CHAT, digest, 86_400, T0) == (10,)


# -- coverage ----------------------------------------------------------------


def test_scan_ranges_are_stored_merged(archive_db):
    """The non-overlapping invariant lives in the table, not only in the code
    that reads it."""
    add_scan_range(archive_db, CHAT, IdRange(1, 10), T0)
    add_scan_range(archive_db, CHAT, IdRange(11, 20), T0)
    add_scan_range(archive_db, CHAT, IdRange(40, 50), T0)
    assert load_scan_ranges(archive_db, CHAT) == (IdRange(1, 20), IdRange(40, 50))
    assert archive_db.execute("SELECT COUNT(*) FROM scan_ranges").fetchone()[0] == 2


def test_recording_the_same_range_twice_changes_nothing(archive_db):
    add_scan_range(archive_db, CHAT, IdRange(1, 10), T0)
    assert add_scan_range(archive_db, CHAT, IdRange(1, 10), T0) == (IdRange(1, 10),)


def test_scan_ranges_are_per_chat(archive_db):
    add_scan_range(archive_db, CHAT, IdRange(1, 10), T0)
    add_scan_range(archive_db, -100999, IdRange(500, 600), T0)
    assert load_scan_ranges(archive_db, CHAT) == (IdRange(1, 10),)
    assert load_scan_ranges(archive_db, -100999) == (IdRange(500, 600),)


def test_meta_round_trips_and_overwrites(archive_db):
    meta_set(archive_db, "k", "one", T0)
    assert meta_get(archive_db, "k") == "one"
    meta_set(archive_db, "k", "two", T0)
    assert meta_get(archive_db, "k") == "two"
    assert meta_get(archive_db, "absent") is None
