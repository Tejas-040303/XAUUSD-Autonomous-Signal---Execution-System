"""Media admission policy and on-disk layout.

Everything here treats the incoming attachment as hostile. The channel is not
ours, anyone who can post to it chooses these bytes and this metadata, and the
downstream consumer is a vision model whose output moves money.

Three separate defences, in the order they apply:

1. **Admission before download.** Telegram hands us the declared MIME type,
   byte size and pixel dimensions *before* any bytes transfer (verified:
   `PhotoSize` carries `w`/`h`/`size`; `Document` carries `mime_type`/`size`
   plus a `DocumentAttributeImageSize`). So a 400-megapixel decompression bomb
   is refused without spending the bandwidth, and a `.exe` posted to the
   channel is never fetched at all. Declared metadata is a filter, not a
   guarantee — hence step 3.

2. **The name the sender chose never reaches the filesystem.**
   `DocumentAttributeFilename.file_name` is attacker-controlled text. A path
   built from it is a traversal bug (`../../.env`), and on Windows it is also a
   reserved-device bug (`CON`, `NUL`, `LPT1`) and an alternate-data-stream bug
   (`x.png:evil`). Paths are therefore derived **only** from ids we assign, and
   the extension comes from the MIME allowlist rather than from the name. The
   declared name is stored as a data column, for the record, and is never used
   for anything.

3. **Verification after download.** Declared size is a claim; the bytes are the
   fact. The stored length is checked against the cap again and against the
   declaration, and the digest is taken from what actually landed.

What this module does NOT do
----------------------------
It does not decode the image, so it cannot verify that the declared dimensions
are honest — a JPEG header can lie, and the real pixel count only becomes known
to a decoder. The decoder arrives with the vision parser in P1; the
`max_image_pixels` ceiling must be re-asserted there, against the decoded
image, because that is the only place the truth is available. Declared
dimensions here reduce the attack surface, they do not close it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

# Screenshots only. A trading signal channel has no reason to send us anything
# else, and every additional format is another decoder's CVE surface reachable
# from a chat we do not control.
ALLOWED_MIME: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

# Directory fan-out for the media tree: ids are bucketed so no single directory
# accumulates unboundedly over years of archiving.
_BUCKET = 1000


class MediaKind(StrEnum):
    PHOTO = "photo"
    DOCUMENT = "document"


class RejectReason(StrEnum):
    """Structured reasons, per CLAUDE.md: never a bare "rejected"."""

    MIME_NOT_ALLOWED = "MIME_NOT_ALLOWED"
    MIME_MISSING = "MIME_MISSING"
    DECLARED_TOO_LARGE = "DECLARED_TOO_LARGE"
    DECLARED_TOO_MANY_PIXELS = "DECLARED_TOO_MANY_PIXELS"
    DECLARED_DIMENSIONS_INVALID = "DECLARED_DIMENSIONS_INVALID"
    STORED_TOO_LARGE = "STORED_TOO_LARGE"
    STORED_EMPTY = "STORED_EMPTY"
    SIZE_MISMATCH = "SIZE_MISMATCH"


@dataclass(frozen=True, slots=True)
class MediaLimits:
    """Admission thresholds. From config; no defaults here on purpose."""

    max_bytes: int
    max_pixels: int
    # Telegram compresses photos, so the transferred size routinely differs
    # from the declared one. A mismatch beyond this fraction is suspicious
    # rather than merely lossy.
    size_mismatch_tolerance: float = 0.25

    def __post_init__(self) -> None:
        if self.max_bytes <= 0 or self.max_pixels <= 0:
            raise ValueError("media limits must be positive")
        if not 0 <= self.size_mismatch_tolerance < 1:
            raise ValueError("size_mismatch_tolerance must be in [0, 1)")


@dataclass(frozen=True, slots=True)
class DeclaredMedia:
    """What Telegram says an attachment is, before we fetch it.

    `name` is untrusted and present only to be stored. Nothing in this module
    passes it to the filesystem.
    """

    kind: MediaKind
    mime: str | None
    size_bytes: int | None
    width: int | None
    height: int | None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class Admission:
    ok: bool
    reason: RejectReason | None = None
    detail: str = ""

    @staticmethod
    def allow() -> Admission:
        return Admission(ok=True)

    @staticmethod
    def deny(reason: RejectReason, detail: str = "") -> Admission:
        return Admission(ok=False, reason=reason, detail=detail)


def admit(declared: DeclaredMedia, limits: MediaLimits) -> Admission:
    """Decide whether to download this attachment at all.

    Fails closed: missing metadata is a rejection, not a pass. A `None` MIME
    type means Telegram did not tell us what this is, and "download it and find
    out" is the wrong resolution for bytes from a channel we do not control.
    """
    if not declared.mime:
        return Admission.deny(RejectReason.MIME_MISSING, f"kind={declared.kind}")
    if declared.mime not in ALLOWED_MIME:
        return Admission.deny(
            RejectReason.MIME_NOT_ALLOWED,
            f"{declared.mime!r} not in {sorted(ALLOWED_MIME)}",
        )
    if declared.size_bytes is not None and declared.size_bytes > limits.max_bytes:
        return Admission.deny(
            RejectReason.DECLARED_TOO_LARGE,
            f"{declared.size_bytes} > {limits.max_bytes}",
        )

    w, h = declared.width, declared.height
    if w is not None and h is not None:
        if w <= 0 or h <= 0:
            return Admission.deny(RejectReason.DECLARED_DIMENSIONS_INVALID, f"{w}x{h}")
        if w * h > limits.max_pixels:
            return Admission.deny(
                RejectReason.DECLARED_TOO_MANY_PIXELS,
                f"{w}x{h} = {w * h} > {limits.max_pixels}",
            )
    return Admission.allow()


def verify_stored(
    declared: DeclaredMedia, stored_bytes: int, limits: MediaLimits
) -> Admission:
    """Re-check what actually landed. The declaration was only ever a claim."""
    if stored_bytes <= 0:
        return Admission.deny(RejectReason.STORED_EMPTY, "0 bytes on disk")
    if stored_bytes > limits.max_bytes:
        return Admission.deny(
            RejectReason.STORED_TOO_LARGE, f"{stored_bytes} > {limits.max_bytes}"
        )
    if declared.size_bytes:
        drift = abs(stored_bytes - declared.size_bytes) / declared.size_bytes
        if drift > limits.size_mismatch_tolerance:
            return Admission.deny(
                RejectReason.SIZE_MISMATCH,
                f"declared {declared.size_bytes}, stored {stored_bytes} ({drift:.0%} drift)",
            )
    return Admission.allow()


def relative_path(chat_id: int, message_id: int, index: int, mime: str) -> PurePosixPath:
    """Where this attachment is stored, relative to the media root.

    Built entirely from values we control: the numeric chat id, the numeric
    message id, the position within the message, and an extension looked up
    from the MIME allowlist. There is no path component the sender can
    influence, so traversal is impossible by construction rather than by
    sanitising.

    `chat_` prefixes the directory because chat ids are negative for channels
    and a path component starting with `-` gets parsed as a flag by a
    surprising number of command-line tools.

    POSIX separators in the stored value so the database is portable; the
    caller joins it onto a real root with `Path`.
    """
    try:
        suffix = ALLOWED_MIME[mime]
    except KeyError as exc:
        raise ValueError(
            f"{mime!r} is not an allowed media type, so it must not have been downloaded. "
            "admit() should have rejected it before this point."
        ) from exc
    if message_id < 1:
        raise ValueError(f"message ids start at 1, got {message_id}")
    if index < 0:
        raise ValueError(f"media index must be non-negative, got {index}")
    bucket = message_id // _BUCKET
    return PurePosixPath(f"chat_{chat_id}") / str(bucket) / f"{message_id}_{index}{suffix}"


def absolute_path(media_root: Path, relative: PurePosixPath) -> Path:
    """Join a stored relative path onto the root, refusing to escape it.

    Defence in depth: `relative_path` cannot produce an escaping path, but this
    also guards the value read back *from the database*, which is where a
    mistake would surface after the fact.
    """
    root = media_root.resolve()
    candidate = (root / Path(relative)).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"media path {relative} escapes the media root {root}")
    return candidate
