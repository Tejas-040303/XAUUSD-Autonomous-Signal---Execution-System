"""Content fingerprinting, including the case that would destroy the corpus.

The bug this file exists to prevent: hashing text only. Every image-only
message then has an empty body, so they all hash identically and dedupe
discards every screenshot after the first — and those screenshots are the corpus
spec §28's parser has to be developed against.

Every invisible character is bound to a named constant below, and control
characters are written as escapes. A bare zero-width space in a test file cannot
be seen in review — the same property that makes it a dedupe-evasion vector — so
naming it is what lets a reader tell which case each assertion covers.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from xauusd.archive.content import (
    HASH_VERSION,
    fingerprint,
    normalize_text,
    rejected_media_token,
    sha256_hex,
)

A = "a" * 64
B = "b" * 64

ZWSP = "​"  # zero-width space
BOM = "﻿"  # byte-order mark
LRM = "‎"  # left-to-right mark
NBSP = " "  # non-breaking space, folded to a plain space by NFKC


# -- the corpus-destroying case ----------------------------------------------


def test_image_only_messages_do_not_collide():
    """Two different screenshots with no caption are two different messages.

    The regression that matters most: under a text-only hash both would be
    `sha256("")` and the second would be discarded as a duplicate.
    """
    assert fingerprint("", (A,)).digest != fingerprint("", (B,)).digest


def test_identical_screenshots_do_collide():
    assert fingerprint("", (A,)).digest == fingerprint("", (A,)).digest


def test_nothing_comparable_is_none_not_the_empty_hash():
    """No text and no media is "unknown", not "equal to every other empty thing".

    A NULL does not match another NULL in SQL, which makes the fail-closed
    behaviour automatic at the query layer rather than a rule every caller has
    to remember.
    """
    fp = fingerprint("")
    assert fp.digest is None
    assert not fp.comparable
    assert fp.digest != sha256_hex(b"")


def test_same_caption_different_image_is_a_different_message():
    """The provider reposting one setup with a corrected chart is not a dup."""
    caption = "XAUUSD BUY 2650 SL 2645"
    assert fingerprint(caption, (A,)).digest != fingerprint(caption, (B,)).digest


def test_media_order_is_part_of_the_identity():
    assert fingerprint("x", (A, B)).digest != fingerprint("x", (B, A)).digest


# -- normalisation -----------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right", "why"),
    [
        ("BUY 2650", "buy 2650", "case"),
        ("BUY  2650", "BUY 2650", "whitespace run"),
        ("  BUY 2650  ", "BUY 2650", "surrounding whitespace"),
        ("BUY\n2650", "BUY 2650", "newline is whitespace"),
        ("BUY\t2650", "BUY 2650", "tab is whitespace"),
        (f"BUY{NBSP}2650", "BUY 2650", "non-breaking space, NFKC then collapse"),
        ("XAUUSD ２６５０", "XAUUSD 2650", "full-width digits"),
        (f"BUY{ZWSP} 2650", "BUY 2650", "zero-width space: cheapest dedupe evasion"),
        (f"{BOM}BUY 2650", "BUY 2650", "byte-order mark"),
        (f"BUY{LRM} 2650", "BUY 2650", "left-to-right mark"),
        ("BUY\x00 2650", "BUY 2650", "NUL control character"),
        ("BUY\x07 2650", "BUY 2650", "bell control character"),
    ],
)
def test_normalisation_folds(left: str, right: str, why: str):
    assert normalize_text(left) == normalize_text(right), why
    assert fingerprint(left).digest == fingerprint(right).digest, why


@pytest.mark.parametrize(
    ("left", "right", "why"),
    [
        ("BUY 2650", "SELL 2650", "direction"),
        ("BUY 2650", "BUY 2651", "price"),
        ("\U0001f534 2650", "\U0001f7e2 2650", "emoji may be the only direction marker"),
        ("SL 2645", "SL 2645.0", "a trailing zero is a different literal"),
        ("TP1 2655 TP2 2665", "TP1 2665 TP2 2655", "target order"),
    ],
)
def test_normalisation_preserves_meaning(left: str, right: str, why: str):
    assert fingerprint(left).digest != fingerprint(right).digest, why


def test_zero_width_padding_cannot_manufacture_a_new_signal():
    """A message padded with invisible characters is the same message.

    Without `Cf` stripping, one injected U+200B yields a "new" signal that
    renders identically to the one already acted on — a duplicate trade for
    free, and the padding is invisible to whoever reviews the log afterwards.
    """
    original = "XAUUSD BUY 2650.50 SL 2645 TP 2660"
    assert fingerprint(ZWSP.join(original)).digest == fingerprint(original).digest


# -- versioning and validation -----------------------------------------------


def test_version_travels_with_the_digest():
    assert fingerprint("BUY").version == HASH_VERSION
    assert fingerprint("", ()).version == HASH_VERSION


@pytest.mark.parametrize("bad", ["", "abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65])
def test_media_digest_must_be_sha256_hex(bad: str):
    """A path or raw bytes passed here would hash to something plausible that
    matches nothing, so it fails loudly instead of quietly."""
    with pytest.raises(ValueError, match="sha256 hex"):
        fingerprint("x", (bad,))


def test_rejected_token_is_stable_and_namespaced():
    """A policy rejection is *known* content, so it hashes deterministically.

    Re-scanning must produce the same fingerprint, or every backfill pass would
    make its own messages look new.
    """
    assert rejected_media_token("MIME_NOT_ALLOWED") == rejected_media_token("MIME_NOT_ALLOWED")
    assert rejected_media_token("MIME_NOT_ALLOWED") != rejected_media_token("DOWNLOAD_FAILED")
    assert rejected_media_token("X") != sha256_hex(b"X")
    # And it is a valid digest, so it satisfies fingerprint()'s validation.
    fingerprint("x", (rejected_media_token("X"),))


# -- properties --------------------------------------------------------------


@pytest.mark.property
@settings(max_examples=200)
@given(st.text())
def test_normalisation_is_idempotent(text: str):
    """Needed for stability across re-scans: if normalising twice differed, the
    same message could produce two hashes on two passes."""
    once = normalize_text(text)
    assert normalize_text(once) == once


@pytest.mark.property
@settings(max_examples=200)
@given(st.text())
def test_fingerprint_is_deterministic(text: str):
    assert fingerprint(text).digest == fingerprint(text).digest


@pytest.mark.property
@settings(max_examples=200)
@given(st.text(), st.lists(st.sampled_from([A, B, "c" * 64]), max_size=4))
def test_comparable_exactly_when_there_is_content(text: str, media: list[str]):
    fp = fingerprint(text, tuple(media))
    assert fp.comparable is (bool(normalize_text(text)) or bool(media))


@pytest.mark.property
@settings(max_examples=100)
@given(st.text(min_size=1).filter(lambda t: normalize_text(t) != ""))
def test_adding_media_always_changes_the_fingerprint(text: str):
    """Otherwise a caption reposted with a screenshot would suppress the
    screenshot — losing the corpus entry we most want."""
    assert fingerprint(text).digest != fingerprint(text, (A,)).digest


@pytest.mark.property
@settings(max_examples=100)
@given(st.lists(st.sampled_from([A, B, "c" * 64]), min_size=2, max_size=4, unique=True))
def test_distinct_media_sets_have_distinct_fingerprints(media: list[str]):
    assert fingerprint("", tuple(media)).digest != fingerprint("", tuple(reversed(media))).digest
