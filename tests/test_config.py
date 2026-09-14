"""Configuration must fail closed.

The point of these tests is that a missing safety value is a startup failure,
not a silently applied default. Centralising limits in one file (spec §38) only
helps if the file cannot be incomplete.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from xauusd.config import ConfigError, load_config
from xauusd.config.schema import Config

VALID = {
    "symbol": {
        "name": "XAUUSD",
        "digits": 2,
        "point": "0.01",
        "pip_size": "0.10",
        "contract_size": "100",
        "volume_min": "0.01",
        "volume_max": "50",
        "volume_step": "0.01",
        "stops_level_points": 50,
        "freeze_level_points": 20,
    },
    "sessions": {
        "morning": {"start": "05:30", "end": "09:00"},
        "evening": {"start": "20:00", "end": "23:30"},
    },
    "risk": {
        "daily_loss_limit": "50",
        "min_sl_pips": "10",
        "max_sl_pips": "70",
        "max_sl_pips_evening": "40",
        "min_rr": "1",
        "max_daily_trades_morning": 3,
        "max_daily_trades_evening": 1,
        "max_entry_distance_pips": "15",
        "max_spread_pips": "8",
        "protective_buffer_pips": "15",
        "expected_slippage_pips": "3",
        "large_target_pips": "170",
        "successful_trade_pips": "70",
        "required_70pip_wins_for_evening": 2,
        "consecutive_losses_to_disable_morning": 3,
    },
    "execution": {
        "magic": 770401,
        "max_order_frequency_seconds": 60,
        "max_deviation_points": 20,
        "orphan_sweep_seconds": 60,
        "tp_poll_seconds": "1",
        "max_tick_age_seconds": "5",
    },
    "telegram": {
        "sources": [{"chat_id": -1001234567890, "kind": "channel", "allowed_sender_ids": []}],
        "status_chat_id": -1009876543210,
        "heartbeat_seconds": 180,
        "max_image_bytes": 8388608,
        "max_image_pixels": 40000000,
        "max_signal_age_seconds": "300",
        "dedupe_window_seconds": 86400,
    },
    "database": {
        "trading_path": "data/trading.db",
        "archive_path": "data/archive.db",
        "busy_timeout_ms": 5000,
    },
}


def write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_the_reference_config_validates(tmp_path: Path):
    cfg = load_config(write(tmp_path, VALID))
    assert isinstance(cfg, Config)
    assert cfg.symbol.pip_size == Decimal("0.10")
    assert cfg.risk.max_sl_pips == Decimal("70")


def test_missing_file_is_an_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_malformed_yaml_is_an_error(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("symbol: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(p)


# ---------------------------------------------------------------------------
# Fail closed: every safety value is required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("symbol", "pip_size"),  # the unresolved blocker — must never default
        ("symbol", "point"),
        ("symbol", "volume_step"),
        ("symbol", "contract_size"),
        ("risk", "daily_loss_limit"),
        ("risk", "max_sl_pips"),
        ("risk", "min_rr"),
        ("risk", "max_daily_trades_morning"),
        ("risk", "max_entry_distance_pips"),
        ("risk", "protective_buffer_pips"),
        ("execution", "max_order_frequency_seconds"),
        ("execution", "magic"),
        ("telegram", "status_chat_id"),
        ("telegram", "max_signal_age_seconds"),
        ("telegram", "dedupe_window_seconds"),
    ],
)
def test_every_safety_value_is_required(tmp_path: Path, section: str, key: str):
    """No default. A missing limit must stop startup, not be quietly supplied."""
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in VALID.items()}
    data[section] = {k: v for k, v in VALID[section].items() if k != key}
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, data))


def test_evening_end_is_required(tmp_path: Path):
    """Spec §4 says the close time is configurable; §38's sample omits it.

    Without it the evening session never closes, which is the failure §4 warns
    about. No default is invented.
    """
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in VALID.items()}
    data["sessions"] = {
        "morning": VALID["sessions"]["morning"],
        "evening": {"start": "20:00"},
    }
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, data))


def test_a_typo_fails_rather_than_leaving_the_real_field_defaulted(tmp_path: Path):
    """`extra="forbid"` is why a typo cannot silently disable a limit."""
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in VALID.items()}
    risk = dict(VALID["risk"])
    risk["max_sl_pip"] = risk.pop("max_sl_pips")  # singular typo
    data["risk"] = risk
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, data))


# ---------------------------------------------------------------------------
# Cross-field coherence
# ---------------------------------------------------------------------------


def test_evening_session_may_not_cross_midnight(tmp_path: Path):
    """A cross-midnight evening would roll DailyState mid-session.

    At 00:00 IST a fresh row starts with evening_consumed = 0, permitting a
    second evening trade inside one continuous session and breaking §4's cap.
    """
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in VALID.items()}
    data["sessions"] = {
        "morning": VALID["sessions"]["morning"],
        "evening": {"start": "20:00", "end": "01:00"},
    }
    with pytest.raises(ConfigError, match="after start"):
        load_config(write(tmp_path, data))


def test_evening_sl_cap_may_not_exceed_the_global_cap(tmp_path: Path):
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in VALID.items()}
    risk = dict(VALID["risk"])
    risk["max_sl_pips_evening"] = "80"
    data["risk"] = risk
    with pytest.raises(ConfigError, match="stricter rule"):
        load_config(write(tmp_path, data))


def test_status_chat_must_differ_from_every_source(tmp_path: Path):
    """Otherwise the bot re-ingests its own status messages as signals."""
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in VALID.items()}
    tg = dict(VALID["telegram"])
    tg["status_chat_id"] = VALID["telegram"]["sources"][0]["chat_id"]
    data["telegram"] = tg
    with pytest.raises(ConfigError, match="re-ingest its own status"):
        load_config(write(tmp_path, data))


def test_a_group_source_must_name_who_may_post(tmp_path: Path):
    """In a group any member can post. Nothing in spec §1-§46 says who may."""
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in VALID.items()}
    tg = dict(VALID["telegram"])
    tg["sources"] = [{"chat_id": -100111, "kind": "group", "allowed_sender_ids": []}]
    data["telegram"] = tg
    with pytest.raises(ConfigError, match="allowed_sender_ids"):
        load_config(write(tmp_path, data))


def test_trading_and_archive_databases_must_be_separate(tmp_path: Path):
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in VALID.items()}
    db = dict(VALID["database"])
    db["archive_path"] = db["trading_path"]
    data["database"] = db
    with pytest.raises(ConfigError, match="must differ"):
        load_config(write(tmp_path, data))


def test_unreachable_staged_exits_are_rejected_at_startup(tmp_path: Path):
    """If buffer + slippage + stops_level >= max_sl_pips, no signal can stage.

    Every trade would silently degenerate to a single exit, so the config is
    incoherent rather than merely conservative.
    """
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in VALID.items()}
    risk = dict(VALID["risk"])
    risk["protective_buffer_pips"] = "60"
    risk["expected_slippage_pips"] = "10"
    data["risk"] = risk
    with pytest.raises(ConfigError, match="staged exits are unreachable"):
        load_config(write(tmp_path, data))


def test_pip_size_must_be_whole_points(tmp_path: Path):
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in VALID.items()}
    sym = dict(VALID["symbol"])
    sym["point"] = "0.03"
    data["symbol"] = sym
    with pytest.raises(ConfigError, match="whole multiple of point"):
        load_config(write(tmp_path, data))
