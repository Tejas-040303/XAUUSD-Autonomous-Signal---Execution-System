"""Configuration schema. Fails closed.

Spec §38 centralises safety-critical values in one place. This schema adds the
property that makes centralisation safe: **no safety value has a default.** A
missing key is a startup failure, never a silently applied fallback, because a
fallback is how a limit you thought you had turns out never to have existed.

`extra="forbid"` throughout, so a typo (`max_sl_pip` for `max_sl_pips`) fails
loudly instead of leaving the real field on its default.

Values still unresolved — the XAUUSD pip definition above all — are required
fields with no default. The system therefore refuses to start until a human
supplies them from the broker's contract specification, which is the intended
behaviour rather than a limitation.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from xauusd.price import SymbolSpec

_STRICT = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SymbolConfig(BaseModel):
    """Mirrors the broker's contract specification. Verified against the live
    broker at startup (spec §36 step 9); a mismatch halts rather than adapts."""

    model_config = _STRICT

    name: str = Field(description="Exact broker symbol string, e.g. XAUUSD or XAUUSD.m")
    digits: int = Field(ge=0, le=10)
    point: Decimal = Field(gt=0, description="Smallest price increment")
    pip_size: Decimal = Field(
        gt=0,
        description=(
            "Price value of ONE PIP. From the broker's contract spec — never guessed. "
            "Every limit in spec §6/§7/§14/§16 is denominated in this unit, so a wrong "
            "value rescales the entire risk model."
        ),
    )
    contract_size: Decimal = Field(gt=0, description="Units per lot (100 oz for XAUUSD)")
    volume_min: Decimal = Field(gt=0)
    volume_max: Decimal = Field(gt=0)
    volume_step: Decimal = Field(gt=0)
    stops_level_points: int = Field(ge=0, description="Minimum stop distance, in points")
    freeze_level_points: int = Field(ge=0, description="Modification-forbidden band, in points")

    def to_spec(self) -> SymbolSpec:
        return SymbolSpec(
            name=self.name,
            digits=self.digits,
            point=self.point,
            pip_size=self.pip_size,
            contract_size=self.contract_size,
            volume_min=self.volume_min,
            volume_max=self.volume_max,
            volume_step=self.volume_step,
            stops_level_points=self.stops_level_points,
            freeze_level_points=self.freeze_level_points,
        )

    @model_validator(mode="after")
    def _coherent(self) -> SymbolConfig:
        # Delegate to SymbolSpec so the invariants live in exactly one place.
        self.to_spec()
        return self


class SessionWindow(BaseModel):
    model_config = _STRICT

    start: time
    end: time

    @model_validator(mode="after")
    def _same_day(self) -> SessionWindow:
        if self.end <= self.start:
            raise ValueError(
                f"session end ({self.end}) must be after start ({self.start}) on the same IST day. "
                "A window crossing midnight would roll DailyState mid-session and reset "
                "evening_consumed, permitting a second evening trade — that needs a configured "
                "day_start_ist first, which is an open decision."
            )
        return self


class SessionsConfig(BaseModel):
    model_config = _STRICT

    morning: SessionWindow
    evening: SessionWindow  # `end` required, no default (spec §4)


class RiskConfig(BaseModel):
    model_config = _STRICT

    daily_loss_limit: Decimal = Field(gt=0, description="Account currency, vs starting-day equity")
    min_sl_pips: Decimal = Field(gt=0)
    max_sl_pips: Decimal = Field(gt=0, description="Reject above — never clamp to fit")
    max_sl_pips_evening: Decimal = Field(gt=0, description="Spec §7")
    min_rr: Decimal = Field(gt=0, description="Measured entry -> FINAL TP, not TP1")
    max_daily_trades_morning: int = Field(ge=0)
    max_daily_trades_evening: int = Field(ge=0)
    max_entry_distance_pips: Decimal = Field(
        gt=0,
        description=(
            "Replaces spec §12's 0.005 relative test, which at gold 2650 is about $13.25 — "
            "3x to 30x the evening SL cap, so it would not catch a stale signal."
        ),
    )
    max_spread_pips: Decimal = Field(gt=0)
    protective_buffer_pips: Decimal = Field(gt=0, description="Spec §14's 15 pips")
    expected_slippage_pips: Decimal = Field(ge=0)
    large_target_pips: Decimal = Field(gt=0, description="Spec §16's staged-exit classifier")
    successful_trade_pips: Decimal = Field(gt=0, description="Spec §6's 70-pip bar")
    required_70pip_wins_for_evening: int = Field(ge=0, description="Spec §7")
    consecutive_losses_to_disable_morning: int = Field(ge=1, description="Spec §8")

    @model_validator(mode="after")
    def _coherent(self) -> RiskConfig:
        if self.min_sl_pips >= self.max_sl_pips:
            raise ValueError("min_sl_pips must be below max_sl_pips")
        if self.max_sl_pips_evening > self.max_sl_pips:
            raise ValueError(
                "max_sl_pips_evening must not exceed max_sl_pips; the evening cap is the stricter rule"
            )
        return self


class ExecutionConfig(BaseModel):
    model_config = _STRICT

    magic: int = Field(ge=0, description="Stamped on every order this bot places")
    max_order_frequency_seconds: int = Field(
        gt=0,
        description="Spec §24. Applies to new ENTRIES only; never throttles a protective move.",
    )
    max_deviation_points: int = Field(ge=0, description="Permitted slippage on a market order")
    orphan_sweep_seconds: int = Field(gt=0, description="Spec §22")
    tp_poll_seconds: Decimal = Field(gt=0, description="TP/SL detection interval while open")
    max_tick_age_seconds: Decimal = Field(
        gt=0, description="Reject any decision made on a tick older than this"
    )


class TelegramSource(BaseModel):
    """One channel or group the bot reads signals from.

    `chat_id` is the numeric id, never an @username: a released username can be
    re-registered by anyone, and a group->supergroup migration changes the id.
    Resolve a username to an id once, by hand, then pin the number here.
    """

    model_config = _STRICT

    chat_id: int
    kind: str = Field(pattern="^(channel|group)$")
    allowed_sender_ids: list[int] = Field(
        description=(
            "Who may originate a tradeable signal. Nothing in spec §1-§46 says. "
            "For a group this must be non-empty. For a broadcast channel it may be "
            "empty, because anonymous admin posts arrive with the channel as sender."
        )
    )

    @model_validator(mode="after")
    def _group_needs_senders(self) -> TelegramSource:
        if self.kind == "group" and not self.allowed_sender_ids:
            raise ValueError(
                f"chat_id {self.chat_id} is a group, so any member can post. "
                "allowed_sender_ids must list who may originate a signal."
            )
        return self


class TelegramConfig(BaseModel):
    model_config = _STRICT

    sources: list[TelegramSource] = Field(min_length=1)
    status_chat_id: int = Field(
        description=(
            "Outbound status/heartbeat destination. MUST differ from every source: "
            "posting into a source makes the bot's own message quoting a price "
            "re-ingestible as a signal."
        )
    )
    heartbeat_seconds: int = Field(gt=0)
    max_image_bytes: int = Field(gt=0)
    max_image_pixels: int = Field(gt=0, description="Decompression-bomb ceiling")
    max_signal_age_seconds: Decimal = Field(
        gt=0,
        description=(
            "Time staleness gate. Spec §12 only checks price proximity, so a replayed "
            "or late-arriving message would otherwise be treated as fresh."
        ),
    )
    dedupe_window_seconds: int = Field(gt=0, description="Spec §23's third component")

    @model_validator(mode="after")
    def _status_chat_is_separate(self) -> TelegramConfig:
        source_ids = {s.chat_id for s in self.sources}
        if self.status_chat_id in source_ids:
            raise ValueError(
                f"status_chat_id ({self.status_chat_id}) is also a signal source. "
                "The bot would re-ingest its own status messages as signals."
            )
        return self


class DatabaseConfig(BaseModel):
    model_config = _STRICT

    trading_path: Path
    archive_path: Path
    busy_timeout_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def _separate_files(self) -> DatabaseConfig:
        if self.trading_path == self.archive_path:
            raise ValueError(
                "trading_path and archive_path must differ: the archive is unbounded and "
                "untrusted, and filling its disk must not stop the ledger from recording losses."
            )
        return self


class Config(BaseModel):
    model_config = _STRICT

    symbol: SymbolConfig
    sessions: SessionsConfig
    risk: RiskConfig
    execution: ExecutionConfig
    telegram: TelegramConfig
    database: DatabaseConfig

    @model_validator(mode="after")
    def _cross_section(self) -> Config:
        utils_spec = self.symbol.to_spec()

        # A protective buffer smaller than the spread means the "protected" stop
        # sits inside spread-spike range and gets swept at the daily break.
        if self.risk.protective_buffer_pips <= 0:
            raise ValueError("protective_buffer_pips must be positive")

        # The staged-exit gate needs TP1 to clear buffer + slippage + stops_level.
        # If that sum already exceeds max_sl_pips, no signal can ever stage.
        stops_pips = Decimal(utils_spec.stops_level_points) / Decimal(utils_spec.points_per_pip)
        floor = self.risk.protective_buffer_pips + self.risk.expected_slippage_pips + stops_pips
        if floor >= self.risk.max_sl_pips:
            raise ValueError(
                f"staged exits are unreachable: protective buffer + slippage + stops_level "
                f"= {floor} pips, which is not below max_sl_pips ({self.risk.max_sl_pips}). "
                "Every signal would degenerate to a single exit."
            )
        return self


class ConfigError(Exception):
    """Raised instead of starting on bad or incomplete configuration."""


def load_config(path: str | Path) -> Config:
    """Read and validate config. Raises rather than returning partial state."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config not found: {p}")
    try:
        raw: Any = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"config is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config must be a mapping at the top level, got {type(raw).__name__}")
    try:
        return Config.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"config failed validation:\n{exc}") from exc
