#!/usr/bin/env python3
"""One-off operator tool: list your Telegram chats with their numeric IDs.

Why this exists
---------------
Config pins sources by **numeric chat_id**, never by @username. A username that
is released can be re-registered by anyone, and a group -> supergroup migration
changes the id silently — either way the bot would end up reading a channel you
did not choose. So the id is resolved once, by hand, and written down.

This also answers a question we cannot answer by guessing: whether the signal
source is a **broadcast channel** (only admins post) or a **megagroup** (any
member can post). That decides whether `allowed_sender_ids` must be filled in,
because in a group anyone who can post can move your money.

Read-only. It never sends a message, never joins anything, never modifies a
chat. It logs in, lists, and exits.

Usage
-----
    python3 -m pip install telethon
    export TELEGRAM_API_ID=12345678
    export TELEGRAM_API_HASH=abc123...
    python3 tools/resolve_chat_ids.py                 # all chats
    python3 tools/resolve_chat_ids.py --filter VIP    # substring match on title

First run asks for your phone number and the login code Telegram sends you
(plus your 2FA password if set). That creates `telegram_resolve.session` in the
current directory.

    !! THAT .session FILE IS A FULL-ACCOUNT CREDENTIAL !!

It grants API access to the account: reading every chat, and sending as you. It
bypasses both the account password and 2FA, and is revoked only by terminating
the session from another device (Settings -> Devices). It is gitignored, but
delete it when you are done here — this script does not need to keep it.

Strongly consider running this from a SEPARATE Telegram account that has joined
the signal channel, not your main one. Automated history reading gets accounts
rate-limited and sometimes banned, and a separate account bounds that damage.

This file lives in tools/ rather than xauusd/ on purpose: it is an operator
utility, not part of the trading system, so it does not count against the rule
that only `xauusd/telegram/ingest.py` may import telethon.
"""

from __future__ import annotations

import argparse
import os
import sys

SESSION_NAME = "telegram_resolve"


def _require_env() -> tuple[int, str]:
    api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    missing = [n for n, v in (("TELEGRAM_API_ID", api_id), ("TELEGRAM_API_HASH", api_hash)) if not v]
    if missing:
        sys.exit(
            f"Missing environment variable(s): {', '.join(missing)}\n"
            "Get them from https://my.telegram.org -> API development tools.\n"
            "Set them in your shell or a gitignored .env — never in the repo, never in chat."
        )
    if not api_id.isdigit():
        sys.exit(f"TELEGRAM_API_ID must be a number, got {api_id!r}")
    return int(api_id), api_hash


def _classify(entity) -> tuple[str, str]:
    """(kind, note) for one dialog entity.

    `kind` matches the config vocabulary: "channel" | "group" | other.

    # VERIFY: telethon.tl.types.Channel carries .broadcast and .megagroup flags,
    # and Chat is the legacy (pre-supergroup) group type. Confirm against
    # https://docs.telethon.dev/en/stable/quick-references/faq.html#how-can-i-get-the-chat-id
    # if a chat here classifies unexpectedly. Telethon could not be installed in
    # the environment this file was written in, so these attribute names are
    # from documentation rather than verified by import.
    """
    cls = type(entity).__name__
    if cls == "Channel":
        if getattr(entity, "megagroup", False):
            return "group", "supergroup — ANY member can post, allowlist REQUIRED"
        if getattr(entity, "broadcast", False):
            return "channel", "broadcast — only admins post"
        return "channel", "channel, neither flag set — inspect before trusting"
    if cls == "Chat":
        return "group", "legacy group — ANY member can post, allowlist REQUIRED"
    if cls == "User":
        return "user", "direct message"
    return cls.lower(), f"unrecognised entity type {cls}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--filter", default="", help="only show chats whose title contains this (case-insensitive)")
    args = parser.parse_args()

    api_id, api_hash = _require_env()

    try:
        from telethon import TelegramClient
        from telethon.utils import get_peer_id
    except ImportError:
        sys.exit("telethon is not installed. Run: python3 -m pip install telethon")

    needle = args.filter.lower()
    rows: list[tuple[int, str, str, str]] = []

    # VERIFY: TelegramClient(session, api_id, api_hash) and the sync context
    # manager that runs .start() interactively are the documented entry points:
    # https://docs.telethon.dev/en/stable/basic/signing-in.html
    with TelegramClient(SESSION_NAME, api_id, api_hash) as client:
        me = client.get_me()
        print(f"Signed in as: {getattr(me, 'username', None) or me.id}\n")

        # VERIFY: iter_dialogs() yields Dialog objects with .entity and .name:
        # https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.dialogs.DialogMethods.iter_dialogs
        for dialog in client.iter_dialogs():
            title = (dialog.name or "").strip() or "(untitled)"
            if needle and needle not in title.lower():
                continue
            kind, note = _classify(dialog.entity)
            # get_peer_id returns the canonical *marked* id — the -100... form
            # that config expects. dialog.id should agree; prefer this one.
            rows.append((get_peer_id(dialog.entity), kind, title, note))

    if not rows:
        print("No chats matched." if needle else "No chats found.")
        return 1

    width = max(len(str(r[0])) for r in rows)
    print(f"{'chat_id'.rjust(width)}  {'kind':<8}  title / note")
    print(f"{'-' * width}  {'-' * 8}  {'-' * 50}")
    for chat_id, kind, title, note in rows:
        print(f"{str(chat_id).rjust(width)}  {kind:<8}  {title}")
        print(f"{' ' * width}  {' ' * 8}  -> {note}")

    print(
        "\nPaste the signal source into config as:\n"
        "\n"
        "telegram:\n"
        "  sources:\n"
        "    - chat_id: <the number above>\n"
        '      kind: "channel"      # or "group"\n'
        "      allowed_sender_ids: []   # REQUIRED non-empty for a group\n"
        "\n"
        "The status chat must be a DIFFERENT chat from every source — otherwise\n"
        "the bot's own status messages quoting a price get re-ingested as signals.\n"
        "\n"
        f"When you are done: delete {SESSION_NAME}.session, and terminate the\n"
        "session from Telegram (Settings -> Devices) to be certain."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
