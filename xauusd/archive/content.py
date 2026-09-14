"""Content hashing for duplicate detection.

Spec §23 requires duplicate handling but never defines what makes two messages
"the same". This module defines it, and the definition is deliberately narrow
about one thing: **content hashing never decides whether a message is
archived.**

The archive is keyed on `(chat_id, message_id)`, which Telegram guarantees
unique per chat. That is the only uniqueness the archive enforces. The hash is
a *lead* for the signal layer ("the provider reposted the same setup 40 seconds
later"), stored alongside the message and never used as a constraint.

Why that separation matters
---------------------------
The obvious implementation — hash the message text, make it unique — destroys
the corpus spec §28 depends on. Every image-only message has empty text, so
they all hash identically and all but the first screenshot is discarded as a
duplicate. The screenshots *are* the training corpus. So:

- media contributes its bytes to the hash, which means two different
  screenshots never collide and two identical ones always do;
- a message with neither text nor media hashes to `None`, not to
  `sha256("")` — "cannot be compared by content" is not "equal to every other
  empty thing", and a NULL in SQLite deliberately does not collide in an index.

Normalisation and evasion
-------------------------
Signals arrive from a channel we do not control, so normalisation has to assume
an adversary as well as a phone keyboard:

- **NFKC** folds full-width and other compatibility forms, so `２６５０` and
  `2650` hash alike. Without it, a keyboard switch reads as a new signal.
- **Format characters (Unicode `Cf`) are stripped.** Zero-width space, joiner,
  BOM and the bidi marks are invisible, so a single injected `U+200B` would
  otherwise produce a "new" message that renders identically. This is the
  cheapest possible dedupe evasion, and the same trick is used to smuggle text
  past a reader's eye in an injection attempt.
- **Control characters are stripped** apart from the whitespace that the
  collapse step handles.
- **Whitespace runs collapse** to one space, then the ends are trimmed.
- **`casefold()`**, not `lower()`: it handles the non-ASCII cases `lower()`
  leaves alone.

Emoji survive, deliberately. A red circle against a green one may be the only
thing distinguishing a sell from a buy in this channel's format, so folding
them away would merge two opposite signals.

The rule is versioned. `HASH_VERSION` is stored next to every hash, so
normalisation can be improved later without silently reinterpreting history —
a stored hash is only comparable to another hash of the same version.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass

# Bump when normalisation or the digest layout changes. Hashes of different
# versions are NOT comparable; a consumer must filter on version, not assume.
HASH_VERSION = 1

_SHA256_HEX_LEN = 64


def normalize_text(text: str) -> str:
    """Fold a message body to its comparison form. See module docstring."""
    # NFKC first: it can itself introduce whitespace (U+00A0 -> U+0020), which
    # the collapse step below then handles. Doing it the other way round leaves
    # a non-breaking space intact and produces two hashes for one message.
    folded = unicodedata.normalize("NFKC", text)

    kept: list[str] = []
    for ch in folded:
        category = unicodedata.category(ch)
        if category == "Cf":
            # Invisible by definition: zero-width space/joiner, BOM, bidi marks.
            continue
        if category == "Cc" and ch not in "\t\n\r":
            continue
        kept.append(ch)

    # str.split() with no argument splits on arbitrary whitespace runs and drops
    # empty fields, which is exactly collapse-and-trim in one step.
    return " ".join("".join(kept).split()).casefold()


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ContentFingerprint:
    """A message's comparison identity.

    `digest` is `None` when the message carries nothing comparable — no text and
    no media. Callers must treat that as "unknown", never as "unique" and never
    as "duplicate".
    """

    digest: str | None
    version: int = HASH_VERSION

    @property
    def comparable(self) -> bool:
        return self.digest is not None


def fingerprint(text: str, media_digests: tuple[str, ...] = ()) -> ContentFingerprint:
    """Comparison identity for one message.

    `media_digests` are the SHA-256 hex digests of the stored media bytes, **in
    the order the media appeared**. Order is part of the identity: an album of
    (chart, then entry table) is not the same message as (entry table, then
    chart), and folding them together would let a reordered repost pass as a
    duplicate of something it does not match.

    Raises rather than guessing if a digest is not a SHA-256 hex string —
    passing a file path or a raw `bytes` here would produce a plausible-looking
    hash that silently matches nothing.
    """
    for digest in media_digests:
        if len(digest) != _SHA256_HEX_LEN or not all(c in "0123456789abcdef" for c in digest):
            raise ValueError(
                f"media digest must be lowercase sha256 hex, got {digest!r}. "
                "Pass the digest of the stored bytes, not a path or the bytes themselves."
            )

    normalized = normalize_text(text)
    if not normalized and not media_digests:
        return ContentFingerprint(digest=None)

    # Canonical, self-describing payload. `sort_keys` and the tight separators
    # make the bytes deterministic; `ensure_ascii` keeps them identical
    # regardless of the platform's default encoding. The version is inside the
    # hashed payload as well as beside it, so two versions cannot collide even
    # if their normalisation output happens to agree.
    payload = json.dumps(
        {"v": HASH_VERSION, "text": normalized, "media": list(media_digests)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return ContentFingerprint(digest=sha256_hex(payload))


def rejected_media_token(reason: str) -> str:
    """A stable stand-in digest for an attachment we deliberately did not fetch.

    A policy rejection is *known* content: we can say exactly what it was and
    why it was refused, and that answer is identical every time the same
    message is re-scanned. So it contributes a deterministic token to the
    fingerprint rather than blocking it, and a text signal that happens to
    carry a stray PDF stays comparable.

    A fetch *failure* is different and must not come through here — unknown
    bytes leave the fingerprint `None`, because a message whose identity cannot
    be computed must not be judged a duplicate of anything.

    Namespaced so the token cannot collide with the digest of real image bytes
    unless those bytes are this exact string.
    """
    return sha256_hex(b"xauusd/rejected-media/v1:" + reason.encode("utf-8"))
