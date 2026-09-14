"""Schema migrations, tracked in `PRAGMA user_version`.

Why a runner rather than a single `schema.sql`
----------------------------------------------
The trading database will hold real money state, so "drop and recreate" stops
being available the moment the first order is logged. And `CREATE TABLE IF NOT
EXISTS` alone is not a migration strategy: it silently does nothing when the
table exists with an *older shape*, which is exactly the case that matters. A
column added to the file without being added to a running deployment produces
an `OperationalError` on the order path.

`user_version` is a 32-bit integer stored in the database header. It costs no
table, cannot be missed by a `SELECT`, and is readable from the sqlite3 CLI
while debugging a live file.

Each migration runs inside `BEGIN IMMEDIATE`, and the version bump is part of
that transaction. SQLite supports transactional DDL, so a migration either
applies completely or not at all — there is no half-migrated file to diagnose at
05:30 IST.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from xauusd.db.store import DatabaseError, Role

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# NNN_name.sql — the number is the schema version the file brings the database
# to, so they must start at 1 and not skip.
_FILENAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str


def discover(role: Role, directory: Path | None = None) -> tuple[Migration, ...]:
    """Load this role's migrations, in order, validating the numbering.

    A gap or a duplicate raises. Both mean two branches added a migration
    independently, and applying them in filename order would give two
    deployments different schemas under the same `user_version` — a divergence
    that shows up much later as an impossible bug report.
    """
    base = (directory or MIGRATIONS_DIR) / str(role)
    if not base.is_dir():
        raise DatabaseError(f"no migrations directory for role {role}: {base}")

    found: list[Migration] = []
    for path in sorted(base.iterdir()):
        if path.name.startswith("."):
            continue
        match = _FILENAME.match(path.name)
        if not match:
            raise DatabaseError(
                f"migration filename {path.name!r} is not NNN_lowercase_name.sql. "
                "Strict naming keeps filename order and version order identical."
            )
        found.append(
            Migration(
                version=int(match.group(1)),
                name=path.stem,
                sql=path.read_text(encoding="utf-8"),
            )
        )

    for expected, migration in enumerate(found, start=1):
        if migration.version != expected:
            raise DatabaseError(
                f"migration versions for {role} must be 1..N with no gaps or duplicates; "
                f"expected {expected:03d}, found {migration.version:03d} ({migration.name}). "
                "Two branches probably added a migration at the same number."
            )
    return tuple(found)


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection, role: Role, directory: Path | None = None) -> int:
    """Apply every pending migration. Returns the resulting version.

    Idempotent: running it on an up-to-date database applies nothing.
    """
    migrations = discover(role, directory)
    version = current_version(conn)
    target = len(migrations)

    if version > target:
        raise DatabaseError(
            f"database is at schema version {version} but this build only knows {target}. "
            "This is an older binary against a newer database — it would read columns "
            "it does not understand. Deploy the newer build rather than downgrading the file."
        )

    for migration in migrations[version:]:
        # The transaction lives INSIDE the script, deliberately.
        #
        # `executescript` commits any transaction that is already open before it
        # runs (and the exact behaviour has varied across Python versions), so
        # wrapping it in `write_transaction` does not make the DDL atomic — it
        # just leaves the outer COMMIT with nothing to commit. Putting
        # BEGIN IMMEDIATE and COMMIT in the script text is what actually gets
        # SQLite's transactional DDL, so a migration that fails half way leaves
        # no partially-migrated file.
        #
        # The version bump is inside the same transaction: `user_version` is a
        # header field and is transactional, so the schema and the number it
        # claims to be can never disagree.
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{migration.sql}\n"
            # Not parameterisable: PRAGMA takes a literal. The value comes from
            # the validated filename pattern, so it is an int by construction.
            f"PRAGMA user_version = {int(migration.version)};\n"
            "COMMIT;\n"
        )
        try:
            conn.executescript(script)
        except Exception as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise DatabaseError(
                f"migration {migration.name} for {role} failed and was rolled back: {exc}"
            ) from exc
        version = migration.version
    return version


def verify(conn: sqlite3.Connection, role: Role, directory: Path | None = None) -> None:
    """Assert the database is fully migrated, without changing it.

    Startup (spec §36) calls this rather than `migrate` so that a deployment
    never silently alters the ledger's shape as a side effect of booting. The
    migration is a deliberate, separate step.
    """
    expected = len(discover(role, directory))
    actual = current_version(conn)
    if actual != expected:
        raise DatabaseError(
            f"{role} database is at schema version {actual}, expected {expected}. "
            "Run the migration step before starting."
        )


def integrity_check(conn: sqlite3.Connection) -> None:
    """Run SQLite's own consistency check.

    Worth doing at startup on the trading database: a corrupted index can make
    a `SELECT` return no rows instead of raising, and on the reconciliation path
    "no open position" is indistinguishable from an untracked live position.
    """
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    problems = [r[0] for r in rows if r[0] != "ok"]
    if problems:
        raise DatabaseError("database integrity check failed: " + "; ".join(problems[:5]))

    # Separate check: foreign_keys=ON only enforces constraints on new writes.
    # Rows written while it was off stay broken and silent.
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise DatabaseError(
            f"{len(violations)} foreign key violation(s) present, e.g. {violations[0]}"
        )
