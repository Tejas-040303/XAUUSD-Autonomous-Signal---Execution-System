"""Credentials, read from the environment and never from the repository.

Separate from `schema.py` on purpose. Configuration describes *how the system
behaves* and is committed as a template; credentials describe *who it is* and
must never be in a file the repository tracks — this repository is public, and
a push cannot be undone: unreachable objects stay retrievable by SHA, forks make
them permanent, and the public Events API archives the push itself.

Nothing here is ever logged, echoed, or included in an exception message. The
errors name the *variable* and where to obtain it, never the value: a
`ValueError` carrying a credential ends up in a traceback, a log aggregator and
an issue report.

Why two Telegram credentials
----------------------------
`api_id`/`api_hash` drive an MTProto **user** session, which is the only way to
read a channel someone else owns and the only way to fetch history at all. The
session file that produces is a full-account bearer credential: it bypasses the
account password and 2FA, and is revoked only by terminating the session from
another device.

The bot token writes status messages. It is a separate credential so that a leak
there is bounded to "someone can post in your status chat" rather than account
takeover, and so the reading path cannot write.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

TELEGRAM_API_ID = "TELEGRAM_API_ID"
TELEGRAM_API_HASH = "TELEGRAM_API_HASH"
TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"

_SOURCES = {
    TELEGRAM_API_ID: "https://my.telegram.org -> API development tools",
    TELEGRAM_API_HASH: "https://my.telegram.org -> API development tools",
    TELEGRAM_BOT_TOKEN: "@BotFather -> /mybots -> API Token",
}


class SecretError(Exception):
    """Raised instead of starting without a credential.

    Never carries a credential value.
    """


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SecretError(
            f"{name} is not set. Obtain it from {_SOURCES.get(name, 'your provider')} "
            "and put it in a gitignored .env or an OS keyring — never in the repository, "
            "never in a chat message."
        )
    return value


@dataclass(frozen=True, slots=True)
class TelegramSecrets:
    """The reading credential pair, plus the separate writing token."""

    api_id: int
    api_hash: str
    bot_token: str

    def __repr__(self) -> str:
        # Overridden so a credential cannot reach a log through an f-string, a
        # `print`, a pytest assertion diff or an exception's repr of locals.
        return "TelegramSecrets(api_id=<redacted>, api_hash=<redacted>, bot_token=<redacted>)"

    __str__ = __repr__


def load_telegram_secrets(*, require_bot_token: bool = True) -> TelegramSecrets:
    """Read Telegram credentials from the environment.

    `require_bot_token=False` for the archiver, which only reads: it needs no
    ability to post, and not loading a token it cannot use keeps the write
    credential out of the reading process entirely.
    """
    raw_id = _require(TELEGRAM_API_ID)
    if not raw_id.isdigit():
        raise SecretError(
            f"{TELEGRAM_API_ID} must be a number. Check it was not swapped with "
            f"{TELEGRAM_API_HASH}, which is a hex string."
        )
    token = _require(TELEGRAM_BOT_TOKEN) if require_bot_token else ""
    return TelegramSecrets(
        api_id=int(raw_id), api_hash=_require(TELEGRAM_API_HASH), bot_token=token
    )


def redact(value: str, keep: int = 0) -> str:
    """A safe stand-in for a credential in diagnostic output.

    `keep=0` by default, so the ordinary case leaks nothing at all. Even a short
    prefix of a bot token identifies the bot, and the numeric prefix of a token
    *is* the bot's user id.
    """
    if not value:
        return "<unset>"
    if keep <= 0:
        return f"<set, {len(value)} chars>"
    return f"{value[:keep]}...<redacted, {len(value)} chars>"
