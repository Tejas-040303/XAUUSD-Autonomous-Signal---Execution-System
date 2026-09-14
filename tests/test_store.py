"""Database durability settings.

The test that matters here is `synchronous=FULL` on the trading database. It is
the setting the whole "ledger before broker" argument rests on, and it is
invisible until a power cut — a `kill -9` test passes without it, because the
OS page cache survives. So it is asserted directly rather than inferred from a
crash test that cannot distinguish the two cases.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from xauusd.db.store import (
    MIN_SQLITE,
    DatabaseError,
    Role,
    assert_pragmas,
    check_sqlite_version,
    connect,
    write_transaction,
)


def test_sqlite_is_new_enough_for_strict_tables():
    """STRICT tables need 3.37. Without them SQLite's default affinity would
    happily store the string '2650' in a REAL price column."""
    check_sqlite_version()
    actual = tuple(int(p) for p in sqlite3.sqlite_version.split("."))
    assert actual >= MIN_SQLITE


# ---------------------------------------------------------------------------
# The durability setting the ledger argument depends on
# ---------------------------------------------------------------------------


def test_trading_database_uses_synchronous_full(tmp_path: Path):
    """`synchronous=FULL`, not WAL's conventional NORMAL.

    Under NORMAL, SQLite does not fsync on commit: a committed `intents` row can
    be rolled back by power loss. Recovery would then read "no row, so the order
    was never sent" for an order that did fill, and send a second one.
    """
    conn = connect(tmp_path / "trading.db", Role.TRADING)
    try:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # 2 == FULL
    finally:
        conn.close()


def test_archive_database_may_use_normal(tmp_path: Path):
    """Losing the last archived Telegram message to a power cut is free — the
    backfill overlap scan re-fetches it. The ledger has no such luxury."""
    conn = connect(tmp_path / "archive.db", Role.ARCHIVE)
    try:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # 1 == NORMAL
    finally:
        conn.close()


@pytest.mark.parametrize("role", [Role.TRADING, Role.ARCHIVE])
def test_wal_and_foreign_keys_are_on(tmp_path: Path, role: Role):
    """Foreign keys are OFF by default in SQLite, so declaring them is not enough."""
    conn = connect(tmp_path / f"{role}.db", role)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_busy_timeout_is_applied(tmp_path: Path):
    """WAL grants concurrent readers, not concurrent writers.

    Without a timeout a second writer gets SQLITE_BUSY immediately — and on the
    order path that means a dropped order, or a retry written after the fact
    that breaks the ledger-before-broker ordering.
    """
    conn = connect(tmp_path / "trading.db", Role.TRADING, busy_timeout_ms=7500)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 7500
    finally:
        conn.close()


def test_pragma_assertion_catches_a_downgraded_connection(tmp_path: Path):
    """Pragmas are per-connection, so it is easy to miss one.

    This is what makes the check worth having: a connection that quietly lost
    FULL must fail loudly rather than trade.
    """
    conn = connect(tmp_path / "trading.db", Role.TRADING)
    try:
        conn.execute("PRAGMA synchronous = NORMAL")  # simulate a missed setting
        with pytest.raises(DatabaseError, match="synchronous"):
            assert_pragmas(conn, Role.TRADING)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Transaction behaviour
# ---------------------------------------------------------------------------


def test_no_implicit_transactions(tmp_path: Path):
    """`isolation_level=None`: transactions are explicit or they do not exist.

    An implicit BEGIN is how a write ends up outside the transaction a caller
    thought it was in.
    """
    conn = connect(tmp_path / "trading.db", Role.TRADING)
    try:
        assert conn.isolation_level is None
    finally:
        conn.close()


def test_write_transaction_commits_on_success(tmp_path: Path):
    conn = connect(tmp_path / "trading.db", Role.TRADING)
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT NOT NULL) STRICT")
        with write_transaction(conn) as tx:
            tx.execute("INSERT INTO t (id, v) VALUES (1, 'a')")
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    finally:
        conn.close()


def test_write_transaction_rolls_back_and_reraises(tmp_path: Path):
    """A half-applied write across fills, positions and daily_state would leave
    exposure and counters disagreeing, so the whole thing must unwind."""
    conn = connect(tmp_path / "trading.db", Role.TRADING)
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT NOT NULL) STRICT")
        with pytest.raises(RuntimeError, match="boom"):
            with write_transaction(conn) as tx:
                tx.execute("INSERT INTO t (id, v) VALUES (1, 'a')")
                raise RuntimeError("boom")
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    finally:
        conn.close()


def test_write_transaction_takes_the_lock_up_front(tmp_path: Path):
    """BEGIN IMMEDIATE, not a deferred transaction.

    A deferred transaction that upgrades read->write after another writer
    committed gets SQLITE_BUSY_SNAPSHOT, which `busy_timeout` does *not* retry —
    and the read-modify-write shape (`SELECT consecutive_losses; UPDATE ...`) is
    exactly that shape.
    """
    path = tmp_path / "trading.db"
    a = connect(path, Role.TRADING, busy_timeout_ms=100)
    b = connect(path, Role.TRADING, busy_timeout_ms=100)
    try:
        a.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT NOT NULL) STRICT")
        with write_transaction(a):
            a.execute("INSERT INTO t (id, v) VALUES (1, 'a')")
            # While A holds the write lock, B cannot acquire it.
            with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
                b.execute("BEGIN IMMEDIATE")
    finally:
        a.close()
        b.close()


def test_strict_tables_reject_wrong_types(tmp_path: Path):
    """Why the schema will use STRICT: without it, a price column accepts text."""
    conn = connect(tmp_path / "trading.db", Role.TRADING)
    try:
        conn.execute("CREATE TABLE p (id INTEGER PRIMARY KEY, price REAL NOT NULL) STRICT")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO p (id, price) VALUES (1, 'not-a-price')")
    finally:
        conn.close()


def test_connect_creates_the_parent_directory(tmp_path: Path):
    target = tmp_path / "nested" / "deeper" / "trading.db"
    conn = connect(target, Role.TRADING)
    try:
        assert target.exists()
    finally:
        conn.close()
