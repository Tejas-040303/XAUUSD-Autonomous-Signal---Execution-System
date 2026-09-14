"""Media admission: what we refuse to download, and where bytes are allowed to land.

Every attachment here is treated as hostile. The channel is not ours, anyone who
can post chooses these bytes and this metadata, and the eventual consumer is a
vision model whose output moves money.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from xauusd.archive.media import (
    ALLOWED_MIME,
    DeclaredMedia,
    MediaKind,
    MediaLimits,
    RejectReason,
    absolute_path,
    admit,
    relative_path,
    verify_stored,
)

LIMITS = MediaLimits(max_bytes=8 * 1024 * 1024, max_pixels=40_000_000)
CHAT = -1001234567890


def declared(**overrides) -> DeclaredMedia:
    base = dict(
        kind=MediaKind.PHOTO,
        mime="image/jpeg",
        size_bytes=100_000,
        width=1280,
        height=720,
        name=None,
    )
    base.update(overrides)
    return DeclaredMedia(**base)


# -- admission ---------------------------------------------------------------


def test_a_normal_screenshot_is_admitted():
    assert admit(declared(), LIMITS).ok


@pytest.mark.parametrize(
    "mime",
    ["application/pdf", "application/octet-stream", "video/mp4", "image/svg+xml", "text/html"],
)
def test_non_image_types_are_refused_before_download(mime: str):
    """A signal channel has no reason to send these, and every extra format is
    another decoder's CVE surface reachable from a chat we do not control.
    `image/svg+xml` in particular is a script-bearing document, not a picture."""
    verdict = admit(declared(mime=mime), LIMITS)
    assert not verdict.ok
    assert verdict.reason is RejectReason.MIME_NOT_ALLOWED


def test_missing_mime_fails_closed():
    """"Download it and find out" is the wrong resolution for bytes from a
    channel we do not control."""
    verdict = admit(declared(mime=None), LIMITS)
    assert verdict.reason is RejectReason.MIME_MISSING


def test_decompression_bomb_is_refused_without_spending_bandwidth():
    """30000x30000 is 900 megapixels in a file that can be a few hundred KB.
    The dimensions arrive before any byte transfers, so this costs nothing."""
    verdict = admit(declared(width=30_000, height=30_000), LIMITS)
    assert verdict.reason is RejectReason.DECLARED_TOO_MANY_PIXELS
    assert "900000000" in verdict.detail


def test_oversized_declaration_is_refused():
    verdict = admit(declared(size_bytes=20 * 1024 * 1024), LIMITS)
    assert verdict.reason is RejectReason.DECLARED_TOO_LARGE


@pytest.mark.parametrize(("w", "h"), [(0, 100), (100, 0), (-1, 100)])
def test_nonsense_dimensions_are_refused(w: int, h: int):
    assert admit(declared(width=w, height=h), LIMITS).reason is (
        RejectReason.DECLARED_DIMENSIONS_INVALID
    )


def test_absent_dimensions_do_not_block_an_allowed_type():
    """Telegram does not always supply dimensions for a document. The pixel
    ceiling is then re-asserted after decoding, in P1 — see the module
    docstring; this layer reduces the attack surface, it does not close it."""
    assert admit(declared(width=None, height=None), LIMITS).ok


# -- post-download verification ----------------------------------------------


def test_stored_bytes_within_tolerance_pass():
    assert verify_stored(declared(), 98_000, LIMITS).ok


def test_empty_file_is_rejected():
    assert verify_stored(declared(), 0, LIMITS).reason is RejectReason.STORED_EMPTY


def test_file_larger_than_the_cap_is_rejected_even_if_declared_small():
    """The declaration was a claim. A transfer that exceeds the cap anyway is
    the case the pre-download check cannot catch."""
    assert verify_stored(declared(size_bytes=1000), 20 * 1024 * 1024, LIMITS).reason is (
        RejectReason.STORED_TOO_LARGE
    )


def test_large_drift_from_the_declaration_is_rejected():
    assert verify_stored(declared(), 5_000, LIMITS).reason is RejectReason.SIZE_MISMATCH


def test_no_declaration_means_no_drift_check():
    assert verify_stored(declared(size_bytes=None), 5_000, LIMITS).ok


# -- path safety -------------------------------------------------------------


def test_sender_filename_never_reaches_the_path():
    """`DocumentAttributeFilename.file_name` is attacker-controlled text. A path
    built from it is a traversal bug, and on Windows also a reserved-device and
    alternate-data-stream bug."""
    hostile = [
        "../../../.env",
        "..\\..\\windows\\system32\\config\\sam",
        "/etc/passwd",
        "CON",
        "NUL",
        "LPT1",
        "x.png:evil",
        "a\x00b.png",
        "‮emoc.png",  # right-to-left override, reverses the displayed name
    ]
    for name in hostile:
        path = relative_path(CHAT, 4242, 0, "image/png")
        assert name not in str(path)
        # The name is not even an input to the function, which is the point:
        # traversal is impossible by construction rather than by sanitising.
        assert str(path) == f"chat_{CHAT}/4/4242_0.png"


def test_path_has_no_leading_dash_component():
    """Channel ids are negative and a path component starting with '-' gets
    parsed as a flag by a surprising number of command-line tools."""
    parts = relative_path(CHAT, 1, 0, "image/jpeg").parts
    assert not any(part.startswith("-") for part in parts)


@pytest.mark.parametrize(("mime", "suffix"), sorted(ALLOWED_MIME.items()))
def test_extension_comes_from_the_allowlist(mime: str, suffix: str):
    assert relative_path(CHAT, 7, 0, mime).suffix == suffix


def test_a_disallowed_type_cannot_be_given_a_path():
    """Defence in depth: if admit() were bypassed, there is still nowhere to
    put the file."""
    with pytest.raises(ValueError, match="not an allowed media type"):
        relative_path(CHAT, 7, 0, "application/pdf")


def test_directories_are_bucketed_so_none_grows_without_bound():
    assert relative_path(CHAT, 4242, 0, "image/jpeg").parts[1] == "4"
    assert relative_path(CHAT, 999, 0, "image/jpeg").parts[1] == "0"
    assert relative_path(CHAT, 1_000_000, 0, "image/jpeg").parts[1] == "1000"


def test_absolute_path_refuses_to_escape_the_root(tmp_path: Path):
    """Guards the value read back FROM the database, which is where a mistake
    would surface long after it was made."""
    with pytest.raises(ValueError, match="escapes the media root"):
        absolute_path(tmp_path, PurePosixPath("../outside.png"))
    with pytest.raises(ValueError, match="escapes the media root"):
        absolute_path(tmp_path, PurePosixPath("a/../../outside.png"))


def test_absolute_path_accepts_a_legitimate_relative_path(tmp_path: Path):
    resolved = absolute_path(tmp_path, relative_path(CHAT, 4242, 0, "image/jpeg"))
    assert resolved.is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize("message_id", [0, -1])
def test_invalid_message_id_rejected(message_id: int):
    with pytest.raises(ValueError, match="start at 1"):
        relative_path(CHAT, message_id, 0, "image/jpeg")


# -- limits ------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 0, "max_pixels": 100},
        {"max_bytes": 100, "max_pixels": 0},
        {"max_bytes": -1, "max_pixels": 100},
    ],
)
def test_limits_must_be_positive(kwargs: dict):
    with pytest.raises(ValueError, match="must be positive"):
        MediaLimits(**kwargs)


@pytest.mark.parametrize("tolerance", [-0.1, 1.0, 1.5])
def test_tolerance_must_be_a_proper_fraction(tolerance: float):
    with pytest.raises(ValueError, match="tolerance"):
        MediaLimits(max_bytes=1, max_pixels=1, size_mismatch_tolerance=tolerance)


# -- properties --------------------------------------------------------------


@pytest.mark.property
@settings(max_examples=300)
@given(
    st.sampled_from(sorted(ALLOWED_MIME)),
    st.integers(1, 5_000_000),
    st.integers(0, 100),
)
def test_every_generated_path_stays_inside_the_root(
    mime: str, message_id: int, index: int
):
    """No combination of ids can produce an escaping or absolute path."""
    path = relative_path(CHAT, message_id, index, mime)
    assert not path.is_absolute()
    assert ".." not in path.parts
    assert len(path.parts) == 3


@pytest.mark.property
@settings(max_examples=300)
@given(st.integers(1, 5_000_000), st.integers(0, 100))
def test_paths_are_unique_per_attachment(message_id: int, index: int):
    """Two attachments must never share a file, or one would overwrite the other
    and the archive's digest would describe bytes that are gone."""
    mine = relative_path(CHAT, message_id, index, "image/jpeg")
    assert mine != relative_path(CHAT, message_id, index + 1, "image/jpeg")
    assert mine != relative_path(CHAT, message_id + 1, index, "image/jpeg")
    assert mine != relative_path(CHAT + 1, message_id, index, "image/jpeg")


@pytest.mark.property
@settings(max_examples=300)
@given(st.integers(1, 60_000_000), st.integers(1, 20_000), st.integers(1, 20_000))
def test_admission_never_accepts_beyond_a_limit(size: int, w: int, h: int):
    """The decision is exactly the conjunction of the limits — no input slips
    through a branch."""
    verdict = admit(declared(size_bytes=size, width=w, height=h), LIMITS)
    assert verdict.ok == (size <= LIMITS.max_bytes and w * h <= LIMITS.max_pixels)


@pytest.mark.property
@settings(max_examples=200)
@given(st.integers(1, 60_000_000))
def test_verification_never_accepts_beyond_the_byte_cap(stored: int):
    verdict = verify_stored(declared(size_bytes=None), stored, LIMITS)
    assert verdict.ok == (stored <= LIMITS.max_bytes)
