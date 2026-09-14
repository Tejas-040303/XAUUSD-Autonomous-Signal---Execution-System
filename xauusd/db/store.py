"""SQLite connection factory, with the durability settings the design depends on.

The execution design's central claim is "ledger before broker, always": an
`intents` row is *committed* before any request leaves the process, which is
what makes crash recovery decidable. That claim is only as strong as what
"committed" means.

In WAL mode the conventional setting is `synchronous=NORMAL`, under which
SQLite **does not fsync on commit** — integrity survives a crash, durability
does not. A transaction committed under WAL+NORMAL can be rolled back by power
loss or a hypervisor kill. Recovery would then apply "no intents row, so the
order was never sent" to an order that *did* fill, and send a second one.

Worse, a `kill -9` test passes under NORMAL because the OS page cache survives,
so the obvious crash test certifies the wrong failure mode. Hence
`synchronous=FULL` on the trading database, asserted on every connection, and a
separate power-loss drill rather than a process-kill test.

Two databases, not one: the archive is unbounded and holds untrusted
third-party content, and filling its disk must not stop the ledger from
recording losses — if `daily_state` writes fail, losses stop being counted and
the kill switch can never trip.
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum
from pathlib import Path

# Partial indexes need 3.8, generated columns 3.31, STRICT tables 3.37. The
# schema uses STRICT (SQLite's default affinity would happily store the string
# '2650' in a REAL price column), so 3.37 is the floor.
MIN_SQLITE = (3, 37, 0)


class Role(StrEnum):
    """Which database this connection is for. Decides durability."""

    TRADING = "trading"
    ARCHIVE = "archive"


# Per-connection pragmas, except journal_mode which is persistent in the file.
_PRAGMAS: dict[Role, dict[str, str]] = {
    Role.TRADING: {
        "journal_mode": "wal",
        # Non-negotiable: see module docstring.
        "synchronous": "FULL",
        "foreign_keys": "ON",
        "trusted_schema": "OFF",
    },
    Role.ARCHIVE: {
        "journal_mode": "wal",
        # Losing the last archived Telegram message to a power cut is free;
        # it will be re-fetched by the backfill overlap scan.
        "synchronous": "NORMAL",
        "foreign_keys": "ON",
        "trusted_schema": "OFF",
    },
}

# Values SQLite reports back as integers.
_NUMERIC_PRAGMA = {
    "synchronous": {"OFF": 0, "NORMAL": 1, "FULL": 2, "EXTRA": 3},
    "foreign_keys": {"ON": 1, "OFF": 0},
    "trusted_schema": {"ON": 1, "OFF": 0},
}


class DatabaseError(Exception):
    """Raised instead of trading on a database we cannot vouch for."""


def check_sqlite_version() -> None:
    actual = tuple(int(part) for part in sqlite3.sqlite_version.split("."))
    if actual < MIN_SQLITE:
        raise DatabaseError(
            f"SQLite {'.'.join(map(str, MIN_SQLITE))}+ required for STRICT tables; "
            f"found {sqlite3.sqlite_version}"
        )


def connect(path: str | Path, role: Role, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    """Open a connection with this role's pragmas applied and verified.

    `busy_timeout` matters because WAL grants concurrent *readers*, not
    concurrent writers: without it a second writer gets `SQLITE_BUSY`
    immediately, and on the order path that means either a dropped order or a
    retry written after the fact that breaks the ledger-before-broker ordering.

    Note `busy_timeout` does not cover `SQLITE_BUSY_SNAPSHOT`, which is what a
    deferred transaction gets when it upgrades from read to write after another
    writer committed. Every read-modify-write must therefore use
    `BEGIN IMMEDIATE` (see `write_transaction`) so the lock is taken up front.
    """
    check_sqlite_version()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # VERIFY: SQLite's WAL mode requires shared memory and does not work over a
    # network filesystem — all processes must be on the same host. If the
    # deployment ever splits (Windows terminal + Python elsewhere), each host
    # needs its own local file; never one file on an SMB/NFS share.
    conn = sqlite3.connect(
        p,
        timeout=busy_timeout_ms / 1000,
        isolation_level=None,  # explicit transactions only; no implicit BEGIN
        check_same_thread=True,
    )
    conn.row_factory = sqlite3.Row

    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    for name, value in _PRAGMAS[role].items():
        conn.execute(f"PRAGMA {name} = {value}")

    try:
        assert_pragmas(conn, role, busy_timeout_ms)
    except DatabaseError:
        conn.close()
        raise
    return conn


def assert_pragmas(conn: sqlite3.Connection, role: Role, busy_timeout_ms: int | None = None) -> None:
    """Verify the pragmas actually took effect.

    Pragmas are per-connection (except journal_mode), so with several
    connections it is easy to miss one — and the one that matters is invisible
    until a power cut. A test asserts this on every connection the app opens.
    """
    for name, expected in _PRAGMAS[role].items():
        actual = conn.execute(f"PRAGMA {name}").fetchone()[0]
        if name in _NUMERIC_PRAGMA:
            want = _NUMERIC_PRAGMA[name][expected]
            if int(actual) != want:
                raise DatabaseError(
                    f"PRAGMA {name} is {actual}, expected {want} ({expected}) for role {role}"
                )
        else:
            if str(actual).lower() != expected.lower():
                raise DatabaseError(
                    f"PRAGMA {name} is {actual!r}, expected {expected!r} for role {role}"
                )

    if busy_timeout_ms is not None:
        actual_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        if int(actual_timeout) != int(busy_timeout_ms):
            raise DatabaseError(
                f"PRAGMA busy_timeout is {actual_timeout}, expected {busy_timeout_ms}"
            )


class write_transaction:
    """Context manager for a write that takes the lock up front.

    `BEGIN IMMEDIATE` rather than a deferred transaction, so `busy_timeout`
    applies to acquiring the write lock instead of surfacing as
    `SQLITE_BUSY_SNAPSHOT` on upgrade. Commits on clean exit, rolls back on any
    exception — a half-applied write across `fills`, `positions` and
    `daily_state` leaves exposure and counters disagreeing.
    """

    __slots__ = ("conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False  # never swallow the exception
