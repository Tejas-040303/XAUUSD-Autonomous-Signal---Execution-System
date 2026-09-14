"""Shared fixtures.

`PIP_CONVENTIONS` is the important one. The XAUUSD pip definition is unresolved
(it must come from the broker's contract specification), so every price test
runs against all three plausible conventions. When the real value arrives, the
suite already proves the arithmetic holds for it — and the table documents the
100x spread between them.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from xauusd.price import PriceUtils, SymbolSpec


def _spec(pip_size: str, digits: int, point: str) -> SymbolSpec:
    return SymbolSpec(
        name="XAUUSD",
        digits=digits,
        point=Decimal(point),
        pip_size=Decimal(pip_size),
        contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("50"),
        volume_step=Decimal("0.01"),
        stops_level_points=0,
        freeze_level_points=0,
    )


# (label, pip_size, digits, point)
PIP_CONVENTIONS = [
    ("pip=0.01", "0.01", 2, "0.01"),
    ("pip=0.10", "0.10", 2, "0.01"),
    ("pip=1.00", "1.00", 2, "0.01"),
]


# Module scope: SymbolSpec is a frozen dataclass and PriceUtils is stateless, so
# sharing one instance across generated inputs is correct — and it is what lets
# hypothesis use these fixtures at all (it rejects function-scoped ones, since it
# cannot reset them between examples).
@pytest.fixture(scope="module", params=PIP_CONVENTIONS, ids=[c[0] for c in PIP_CONVENTIONS])
def spec(request):
    _label, pip_size, digits, point = request.param
    return _spec(pip_size, digits, point)


@pytest.fixture(scope="module")
def utils(spec) -> PriceUtils:
    return PriceUtils(spec)


@pytest.fixture(scope="module")
def broker_spec() -> SymbolSpec:
    """A single realistic spec with non-zero broker levels, for stop-distance tests."""
    return SymbolSpec(
        name="XAUUSD",
        digits=2,
        point=Decimal("0.01"),
        pip_size=Decimal("0.10"),
        contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("50"),
        volume_step=Decimal("0.01"),
        stops_level_points=50,  # 5 pips at 0.10/pip
        freeze_level_points=20,  # 2 pips
    )


# -- archiver fixtures --------------------------------------------------------


@pytest.fixture
def archive_db(tmp_path):
    """A migrated archive database. Function-scoped: these tests mutate it."""
    from xauusd.db.migrate import migrate
    from xauusd.db.store import Role, connect

    conn = connect(tmp_path / "archive.db", Role.ARCHIVE)
    migrate(conn, Role.ARCHIVE)
    yield conn
    conn.close()


@pytest.fixture
def trading_db(tmp_path):
    """A migrated trading database, with synchronous=FULL asserted by connect()."""
    from xauusd.db.migrate import migrate
    from xauusd.db.store import Role, connect

    conn = connect(tmp_path / "trading.db", Role.TRADING)
    migrate(conn, Role.TRADING)
    yield conn
    conn.close()


@pytest.fixture
def limits():
    from xauusd.archive.media import MediaLimits

    return MediaLimits(max_bytes=8 * 1024 * 1024, max_pixels=40_000_000)


@pytest.fixture
def frozen_clock():
    """A clock stopped at a morning-session instant (05:45 IST = 00:15 UTC)."""
    from datetime import datetime, timezone

    from xauusd.clock import FakeClock

    return FakeClock(at=datetime(2026, 9, 14, 0, 15, tzinfo=timezone.utc))


@pytest.fixture
def media_root(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    return root
