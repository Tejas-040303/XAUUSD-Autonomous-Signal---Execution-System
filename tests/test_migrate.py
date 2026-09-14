"""Schema migrations: atomicity, ordering discipline, and refusing to guess.

The property that matters: the trading database holds money state, so "drop and
recreate" stops being available the moment the first order is logged. A
half-applied migration has to be impossible rather than merely unlikely.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xauusd.db.migrate import (
    current_version,
    discover,
    integrity_check,
    migrate,
    verify,
)
from xauusd.db.store import DatabaseError, Role, connect


def fresh(tmp_path: Path, role: Role):
    return connect(tmp_path / f"{role}.db", role)


# -- applying ----------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.ARCHIVE, Role.TRADING])
def test_migrations_apply_from_zero(tmp_path: Path, role: Role):
    conn = fresh(tmp_path, role)
    try:
        assert current_version(conn) == 0
        assert migrate(conn, role) == len(discover(role))
        verify(conn, role)
    finally:
        conn.close()


@pytest.mark.parametrize("role", [Role.ARCHIVE, Role.TRADING])
def test_migrating_twice_changes_nothing(tmp_path: Path, role: Role):
    conn = fresh(tmp_path, role)
    try:
        first = migrate(conn, role)
        assert migrate(conn, role) == first
    finally:
        conn.close()


@pytest.mark.parametrize("role", [Role.ARCHIVE, Role.TRADING])
def test_every_table_is_strict(tmp_path: Path, role: Role):
    """Without STRICT, SQLite's default affinity happily stores the string
    '2650' in an INTEGER column and 'null' in a timestamp. The archive is
    exactly where that rot would go unnoticed for months."""
    conn = fresh(tmp_path, role)
    try:
        migrate(conn, role)
        lax = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM pragma_table_list WHERE schema = 'main' AND type = 'table' "
                "AND strict = 0 AND name NOT LIKE 'sqlite_%'"
            )
        ]
        assert lax == []
    finally:
        conn.close()


@pytest.mark.parametrize("role", [Role.ARCHIVE, Role.TRADING])
def test_a_migrated_database_passes_its_own_integrity_check(tmp_path: Path, role: Role):
    conn = fresh(tmp_path, role)
    try:
        migrate(conn, role)
        integrity_check(conn)
    finally:
        conn.close()


# -- refusing to guess -------------------------------------------------------


def test_an_unmigrated_database_is_refused_rather_than_upgraded(tmp_path: Path):
    """Booting must never alter the ledger's shape as a side effect. The
    migration is a deliberate, separate step."""
    conn = fresh(tmp_path, Role.TRADING)
    try:
        with pytest.raises(DatabaseError, match="expected"):
            verify(conn, Role.TRADING)
    finally:
        conn.close()


def test_a_newer_database_than_the_build_is_refused(tmp_path: Path):
    """An older binary against a newer database would read columns it does not
    understand. Deploy the newer build rather than downgrading the file."""
    conn = fresh(tmp_path, Role.TRADING)
    try:
        migrate(conn, Role.TRADING)
        conn.execute("PRAGMA user_version = 99")
        with pytest.raises(DatabaseError, match="older binary"):
            migrate(conn, Role.TRADING)
    finally:
        conn.close()


# -- atomicity ---------------------------------------------------------------


def test_a_failing_migration_leaves_nothing_behind(tmp_path: Path):
    """`executescript` commits any open transaction before running, so the
    transaction has to live INSIDE the script. Without that, the first statement
    of a broken migration would be committed and the file left half-migrated at
    version 0 — the worst of both states."""
    directory = tmp_path / "m" / "archive"
    directory.mkdir(parents=True)
    (directory / "001_init.sql").write_text(
        "CREATE TABLE good (a INTEGER) STRICT;\nCREATE TABLE bad (a NOT_A_TYPE) STRICT;\n"
    )
    conn = connect(tmp_path / "x.db", Role.ARCHIVE)
    try:
        with pytest.raises(DatabaseError, match="rolled back"):
            migrate(conn, Role.ARCHIVE, directory=tmp_path / "m")
        assert current_version(conn) == 0
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        assert "good" not in tables
        assert not conn.in_transaction
    finally:
        conn.close()


# -- numbering discipline ----------------------------------------------------


def test_a_version_gap_is_refused(tmp_path: Path):
    """A gap or duplicate means two branches added a migration independently,
    and filename order would give two deployments different schemas under the
    same user_version — a divergence that surfaces much later as an impossible
    bug report."""
    directory = tmp_path / "m" / "archive"
    directory.mkdir(parents=True)
    (directory / "001_a.sql").write_text("CREATE TABLE a (x INTEGER) STRICT;")
    (directory / "003_c.sql").write_text("CREATE TABLE c (x INTEGER) STRICT;")
    with pytest.raises(DatabaseError, match="no gaps or duplicates"):
        discover(Role.ARCHIVE, directory=tmp_path / "m")


@pytest.mark.parametrize("name", ["1_init.sql", "001-init.sql", "001_Init.sql", "init.sql"])
def test_a_misnamed_migration_is_refused(tmp_path: Path, name: str):
    """Strict naming keeps filename order and version order identical."""
    directory = tmp_path / "m" / "archive"
    directory.mkdir(parents=True)
    (directory / name).write_text("SELECT 1;")
    with pytest.raises(DatabaseError, match="NNN_lowercase_name"):
        discover(Role.ARCHIVE, directory=tmp_path / "m")


def test_a_missing_directory_is_refused(tmp_path: Path):
    with pytest.raises(DatabaseError, match="no migrations directory"):
        discover(Role.ARCHIVE, directory=tmp_path / "absent")


@pytest.mark.parametrize("role", [Role.ARCHIVE, Role.TRADING])
def test_the_shipped_migrations_are_well_numbered(role: Role):
    found = discover(role)
    assert [m.version for m in found] == list(range(1, len(found) + 1))
