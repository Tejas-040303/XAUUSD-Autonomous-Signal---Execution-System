"""Telethon -> `IncomingMessage` conversion, against real library objects.

Skipped when the `telegram` extra is absent, which is the normal state for the
trading-logic test run. When telethon IS installed these are the tests that
stop a library upgrade from silently changing what gets archived: they build
genuine `types.Message` objects offline, with no client and no network.

CLAUDE.md forbids fabricating library behaviour. Everything asserted here was
verified against telethon 1.45 rather than assumed:

- `min_id`/`max_id` on `iter_messages` are **exclusive** (tested in
  `test_ranges.py`);
- `types.MessageService` **subclasses** `Message`, so the service check has to
  come first or every join and pin is archived as a signal;
- `utils.get_peer_id` returns the **marked** id (`-100…` for a channel), which
  is what config pins;
- `Photo.sizes` carries `w`/`h`/`size` before any download, and
  `PhotoSizeProgressive` declares a *list* of byte lengths whose maximum is the
  full image.
"""

from __future__ import annotations

import datetime as dt

import pytest

telethon = pytest.importorskip(
    "telethon", reason="the `telegram` extra is not installed; conversion tests need it"
)

from telethon import utils  # noqa: E402
from telethon.tl import types  # noqa: E402

from xauusd.telegram.ingest import to_incoming  # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 9, 14, 5, 45, tzinfo=UTC)
PEER = types.PeerChannel(channel_id=1234567890)
CHAT = utils.get_peer_id(PEER)


def convert(message):
    return to_incoming(message, CHAT, types, utils)


def message(**overrides):
    base = dict(id=100, peer_id=PEER, date=T0, message="XAUUSD BUY 2650.50 SL 2645 TP1 2655")
    base.update(overrides)
    return types.Message(**base)


def photo(sizes):
    return types.Photo(
        id=1, access_hash=2, file_reference=b"", date=T0, dc_id=2, has_stickers=False, sizes=sizes
    )


# -- identity ----------------------------------------------------------------


def test_the_marked_chat_id_is_used():
    """An unmarked id would make a user id and a channel id collide, and
    `allowed_sender_ids` is compared against the marked form."""
    assert CHAT == -1001234567890
    assert convert(message()).chat_id == -1001234567890


def test_core_fields_are_carried_over():
    converted = convert(
        message(
            from_id=types.PeerUser(user_id=777),
            reply_to=types.MessageReplyHeader(reply_to_msg_id=99),
        )
    )
    assert converted.message_id == 100
    assert converted.sender_id == 777
    assert converted.reply_to_id == 99, "spec §18's highest-confidence correlation link"
    assert converted.text.startswith("XAUUSD BUY")
    assert converted.posted_at == T0


def test_an_anonymous_channel_post_has_no_sender():
    """Which is why `allowed_sender_ids` may legitimately be empty for a
    broadcast channel but never for a group."""
    assert convert(message(post=True)).sender_id is None


def test_timestamps_arrive_timezone_aware():
    """`IncomingMessage` rejects naive input, so a library change that dropped
    tzinfo would fail here rather than shifting every IST session boundary."""
    converted = convert(message())
    assert converted.posted_at.tzinfo is not None


def test_flags_are_mapped():
    converted = convert(
        message(
            edit_date=T0 + dt.timedelta(minutes=3),
            grouped_id=42,
            fwd_from=types.MessageFwdHeader(date=T0),
            out=True,
        )
    )
    assert converted.edited_at == T0 + dt.timedelta(minutes=3)
    assert converted.grouped_id == 42, "album members are separate messages sharing this"
    assert converted.is_forwarded
    assert converted.is_outgoing, "our own message must never be read back as a signal"


# -- message kinds -----------------------------------------------------------


def test_a_service_message_is_detected_despite_subclassing_message():
    """`types.MessageService` inherits from `Message` in telethon, so an
    `isinstance` check in the wrong order archives every join and pin as a
    normal message."""
    converted = convert(
        types.MessageService(
            id=105,
            peer_id=PEER,
            date=T0,
            action=types.MessageActionChatJoinedByLink(inviter_id=5),
        )
    )
    assert converted.is_service
    assert converted.media == ()


def test_message_empty_is_skipped():
    """Telegram's placeholder for a deleted or inaccessible message. Nothing to
    archive — but the surrounding range is still marked scanned, which is the
    scanned-and-absent versus never-looked-at distinction."""
    assert convert(types.MessageEmpty(id=106, peer_id=PEER)) is None


# -- media -------------------------------------------------------------------


def test_the_largest_rendition_is_the_one_measured():
    """Telethon downloads the largest size by default, so that is the one whose
    limits must be checked."""
    media = convert(
        message(
            message="",
            media=types.MessageMediaPhoto(
                photo=photo(
                    [
                        types.PhotoStrippedSize(type="i", bytes=b"\x01\x02"),
                        types.PhotoSize(type="m", w=320, h=180, size=9_000),
                        types.PhotoSizeProgressive(
                            type="y", w=1280, h=720, sizes=[1_000, 20_000, 64_000]
                        ),
                    ]
                )
            ),
        )
    ).media
    assert len(media) == 1
    assert (media[0].width, media[0].height) == (1280, 720)
    assert media[0].size_bytes == 64_000, "a progressive JPEG's full image is the largest prefix"
    assert media[0].mime == "image/jpeg", "Telegram re-encodes every photo to JPEG"


def test_the_blur_placeholder_is_not_mistaken_for_an_image():
    """`PhotoStrippedSize` is a few bytes of blur, never a real rendition."""
    media = convert(
        message(
            message="",
            media=types.MessageMediaPhoto(
                photo=photo([types.PhotoStrippedSize(type="i", bytes=b"\x01\x02")])
            ),
        )
    ).media
    assert media[0].width is None and media[0].size_bytes is None


def test_a_document_declares_its_own_type_and_untrusted_name():
    media = convert(
        message(
            media=types.MessageMediaDocument(
                document=types.Document(
                    id=3,
                    access_hash=4,
                    file_reference=b"",
                    date=T0,
                    mime_type="image/png",
                    size=120_000,
                    dc_id=2,
                    attributes=[
                        types.DocumentAttributeImageSize(w=1920, h=1080),
                        types.DocumentAttributeFilename(file_name="../../../.env"),
                    ],
                )
            )
        )
    ).media
    assert media[0].mime == "image/png"
    assert (media[0].width, media[0].height) == (1920, 1080)
    # Captured as evidence. `media.relative_path` never takes it as an input.
    assert media[0].name == "../../../.env"


def test_a_link_preview_is_not_treated_as_a_screenshot():
    """Telegram generates these from a URL in the text. Treating one as a signal
    screenshot would let any link posted in the channel inject an image into the
    corpus."""
    preview = types.MessageMediaWebPage(
        webpage=types.WebPage(
            id=1,
            url="http://example.invalid",
            display_url="example.invalid",
            hash=0,
            photo=photo([types.PhotoSize(type="y", w=1280, h=720, size=50_000)]),
        )
    )
    assert convert(message(media=preview)).media == ()


@pytest.mark.parametrize(
    ("label", "media"),
    [
        (
            "contact",
            types.MessageMediaContact(
                phone_number="1", first_name="a", last_name="b", vcard="", user_id=1
            ),
        ),
        ("geo", types.MessageMediaGeo(geo=types.GeoPoint(long=1.0, lat=2.0, access_hash=0))),
        ("unsupported", types.MessageMediaUnsupported()),
        ("empty photo", types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1))),
    ],
)
def test_non_screenshot_media_yields_no_attachment_but_is_still_archived(label, media):
    """Recorded as a message with no media rather than dropped, so the id stays
    covered and the coverage record stays honest."""
    converted = convert(message(media=media))
    assert converted is not None, label
    assert converted.media == (), label
