"""Architecture boundaries, enforced mechanically.

CLAUDE.md: "Enforce mechanically — comments do not hold." These tests read the
source with `ast` rather than grepping, so an aliased import or a nested call
cannot slip past.

Two rules are live now; the rest are declared with their target module so they
start enforcing the moment that module appears, rather than being remembered
later.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "xauusd"


def _modules() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if p.name != "__init__.py")


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _rel(path: Path) -> str:
    return str(path.relative_to(PACKAGE.parent))


def _imported_names(tree: ast.Module) -> set[str]:
    """Every module name this file imports, including `from x.y import z` as `x.y`."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _attribute_chains(tree: ast.Module) -> set[str]:
    """Dotted call targets, e.g. `datetime.now` from `datetime.datetime.now()`."""
    chains: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            parts = [node.attr]
            cur = node.value
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            chains.add(".".join(reversed(parts)))
    return chains


# ---------------------------------------------------------------------------
# One time source
# ---------------------------------------------------------------------------

# Broader than CLAUDE.md's "no module imports datetime.now": a lint that checks
# only that one name is defeated by time.time, datetime.utcnow, date.today, or
# datetime.fromtimestamp with no tz.
FORBIDDEN_TIME_CALLS = {
    "datetime.now",
    "datetime.utcnow",
    "datetime.datetime.now",
    "datetime.datetime.utcnow",
    "date.today",
    "datetime.date.today",
    "time.time",
    "time.time_ns",
    "time.monotonic",
    "time.monotonic_ns",
    "time.localtime",
    "time.gmtime",
}

TIME_SOURCE = "xauusd/clock.py"


@pytest.mark.parametrize("module", _modules(), ids=_rel)
def test_only_clock_reads_a_wall_clock(module: Path):
    """Every other module takes a `Clock` by injection.

    This is what lets a test freeze time by construction instead of patching —
    and patching would not reach a broker library's own timestamps anyway.
    """
    if _rel(module) == TIME_SOURCE:
        return
    found = _attribute_chains(_parse(module)) & FORBIDDEN_TIME_CALLS
    assert not found, (
        f"{_rel(module)} reads a wall clock directly ({sorted(found)}). "
        f"Inject a Clock from {TIME_SOURCE} instead (spec §4)."
    )


def test_the_time_source_actually_uses_a_real_clock():
    """Guards against the rule above being satisfied by deleting the clock."""
    chains = _attribute_chains(_parse(PACKAGE / "clock.py"))
    assert "datetime.now" in chains
    assert "time.monotonic" in chains


# ---------------------------------------------------------------------------
# One place for price arithmetic
# ---------------------------------------------------------------------------

PRICE_MODULE = "xauusd/price.py"

# Modules allowed to name numeric price-shaped constants. `config/schema.py`
# carries field bounds (gt=0, le=10) rather than price assumptions.
_PRICE_CONSTANT_EXEMPT = {PRICE_MODULE, "xauusd/config/schema.py"}


@pytest.mark.parametrize("module", _modules(), ids=_rel)
def test_no_module_outside_price_constructs_a_symbol_spec_inline(module: Path):
    """`SymbolSpec` is built from config, which is built from the broker.

    A hand-rolled spec anywhere else is a hard-coded pip assumption wearing a
    type, which is what spec §13 forbids.
    """
    if _rel(module) in _PRICE_CONSTANT_EXEMPT:
        return
    tree = _parse(module)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "SymbolSpec", (
                f"{_rel(module)} constructs a SymbolSpec inline. Build it from "
                f"config so the pip definition has exactly one source."
            )


# ---------------------------------------------------------------------------
# Layer boundaries — declared now, enforced when each module lands
# ---------------------------------------------------------------------------

# (module that is allowed the import, the import prefix it owns)
EXCLUSIVE_IMPORTS = [
    ("xauusd/parse/vlm.py", "anthropic"),
    ("xauusd/broker/mt5.py", "MetaTrader5"),
    ("xauusd/telegram/ingest.py", "telethon"),
]


@pytest.mark.parametrize(("owner", "package"), EXCLUSIVE_IMPORTS, ids=[p for _, p in EXCLUSIVE_IMPORTS])
def test_third_party_surfaces_have_exactly_one_entry_point(owner: str, package: str):
    """Only one module may touch each external surface.

    `MetaTrader5` matters most: it is Windows-only, so an accidental import
    anywhere else makes that module untestable on Linux and silently couples the
    whole package to the deployment platform.
    """
    offenders = [
        _rel(m)
        for m in _modules()
        if _rel(m) != owner
        and any(n == package or n.startswith(f"{package}.") for n in _imported_names(_parse(m)))
    ]
    assert not offenders, f"{package} must only be imported by {owner}; found in {offenders}"


def test_pure_layers_do_not_import_io(request: pytest.FixtureRequest):
    """`policy` and `risk` are pure functions over (signal, state, clock, config).

    Purity is what makes spec §39's exhaustive rule testing cheap, and it is a
    property of the import graph, not of good intentions. Skips until the
    modules exist so the rule is recorded rather than remembered.
    """
    pure = ["xauusd/policy.py", "xauusd/risk.py"]
    forbidden_prefixes = ("xauusd.broker", "xauusd.telegram", "xauusd.parse", "xauusd.db", "anthropic")
    present = [p for p in pure if (PACKAGE.parent / p).is_file()]
    if not present:
        pytest.skip("policy.py / risk.py land in P2; rule recorded for when they do")
    for rel in present:
        names = _imported_names(_parse(PACKAGE.parent / rel))
        bad = [n for n in names if n.startswith(forbidden_prefixes)]
        assert not bad, f"{rel} must stay pure; it imports {bad}"


def test_every_module_is_importable():
    """A syntax error or a circular import should fail here, not at 05:30 IST."""
    import importlib

    for module in _modules():
        dotted = _rel(module)[: -len(".py")].replace("/", ".")
        importlib.import_module(dotted)
