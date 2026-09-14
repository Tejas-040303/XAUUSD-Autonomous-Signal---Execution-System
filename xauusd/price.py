"""All pip, point, spread, volume and stop-distance arithmetic.

Spec §13 forbids assuming `1 pip == 1 point` and requires one central price
utility. CLAUDE.md adds: no module outside this one may contain a numeric price
constant.

The pip definition is **injected**, never assumed. `SymbolSpec.pip_size` comes
from the broker's contract specification via required config with no default,
so the system fails closed until a real value is supplied rather than running
on a guess. That is why the unresolved pip question does not block building
this module — only running it.

Decimal throughout, never float. Prices and volumes live on integer grids
(`point` and `volume_step`); float arithmetic drifts off those grids and makes
`remainder == 0` and `remainder >= volume_min` comparisons answer wrongly, which
spec §17 forbids resolving by silently over-closing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Literal


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """The broker's contract specification for one instrument.

    Every field is read from the broker (or from config mirroring it) and
    verified at startup. Nothing here has a default: a wrong value silently
    rescales every risk limit in the system.
    """

    name: str
    digits: int
    point: Decimal
    pip_size: Decimal
    contract_size: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    stops_level_points: int
    freeze_level_points: int

    def __post_init__(self) -> None:
        if self.point <= 0 or self.pip_size <= 0:
            raise ValueError("point and pip_size must be positive")
        if self.volume_step <= 0:
            raise ValueError("volume_step must be positive")
        if not (0 < self.volume_min <= self.volume_max):
            raise ValueError("require 0 < volume_min <= volume_max")
        if self.digits < 0:
            raise ValueError("digits must be >= 0")
        if self.stops_level_points < 0 or self.freeze_level_points < 0:
            raise ValueError("level distances are counts of points and cannot be negative")

        # A pip that is not a whole number of points means one of the two values
        # is wrong. Catching it here beats discovering it as a rounding drift in
        # a risk calculation.
        ratio = self.pip_size / self.point
        if ratio != ratio.to_integral_value() or ratio < 1:
            raise ValueError(
                f"pip_size ({self.pip_size}) must be a whole multiple of point ({self.point}); "
                f"got {ratio} points per pip"
            )
        # volume_min must itself sit on the step grid, or no normalised volume can.
        steps = self.volume_min / self.volume_step
        if steps != steps.to_integral_value():
            raise ValueError(
                f"volume_min ({self.volume_min}) is not a multiple of volume_step ({self.volume_step})"
            )

    @property
    def points_per_pip(self) -> int:
        return int(self.pip_size / self.point)


class StopVerdict(StrEnum):
    OK = "OK"
    TOO_CLOSE = "TOO_CLOSE"
    WRONG_SIDE = "WRONG_SIDE"
    FROZEN = "FROZEN"


@dataclass(frozen=True, slots=True)
class StopCheck:
    verdict: StopVerdict
    distance_points: int
    required_points: int

    @property
    def ok(self) -> bool:
        return self.verdict is StopVerdict.OK


class PartialCloseVerdict(StrEnum):
    PARTIAL = "PARTIAL"
    TOO_SMALL_TO_CLOSE = "TOO_SMALL_TO_CLOSE"
    REMAINDER_BELOW_MIN = "REMAINDER_BELOW_MIN"


@dataclass(frozen=True, slots=True)
class PartialClose:
    verdict: PartialCloseVerdict
    volume: Decimal
    remainder: Decimal

    @property
    def ok(self) -> bool:
        return self.verdict is PartialCloseVerdict.PARTIAL


class PriceUtils:
    """Pure arithmetic over one `SymbolSpec`. No I/O, no clock, no broker."""

    __slots__ = ("spec",)

    def __init__(self, spec: SymbolSpec) -> None:
        self.spec = spec

    # -- pip <-> price ------------------------------------------------------

    def pips_to_price(self, pips: Decimal | int | str) -> Decimal:
        """A pip count as a price delta."""
        return self._normalize_delta(Decimal(str(pips)) * self.spec.pip_size)

    def price_to_pips(self, delta: Decimal | int | str) -> Decimal:
        """A price delta as a pip count. Sign is preserved."""
        return Decimal(str(delta)) / self.spec.pip_size

    def points_to_pips(self, points: int) -> Decimal:
        return Decimal(points) / Decimal(self.spec.points_per_pip)

    def pips_to_points(self, pips: Decimal | int | str) -> int:
        exact = Decimal(str(pips)) * Decimal(self.spec.points_per_pip)
        return int(exact.to_integral_value(rounding=ROUND_HALF_UP))

    # -- price grid ---------------------------------------------------------

    def normalize_price(self, price: Decimal | int | str) -> Decimal:
        """Snap a price to the broker's point grid and digit count."""
        return self.points_to_price(self.price_to_points(price))

    def price_to_points(self, price: Decimal | int | str) -> int:
        value = Decimal(str(price)) / self.spec.point
        return int(value.to_integral_value(rounding=ROUND_HALF_UP))

    def points_to_price(self, points: int) -> Decimal:
        return self._normalize_delta(Decimal(points) * self.spec.point)

    def _normalize_delta(self, value: Decimal) -> Decimal:
        quantum = Decimal(1).scaleb(-self.spec.digits)
        return value.quantize(quantum)

    # -- volume grid --------------------------------------------------------

    def volume_to_steps(self, volume: Decimal | int | str) -> int:
        value = Decimal(str(volume)) / self.spec.volume_step
        return int(value.to_integral_value(rounding=ROUND_HALF_UP))

    def steps_to_volume(self, steps: int) -> Decimal:
        return (Decimal(steps) * self.spec.volume_step).normalize() + Decimal(0)

    def normalize_volume(
        self,
        volume: Decimal | int | str,
        rounding: Literal["down", "nearest"] = "down",
    ) -> Decimal | None:
        """Snap a volume to the step grid, or return None if it cannot be traded.

        Rounds **down** by default, and that default is load-bearing: rounding a
        close up over-closes and rounding an entry up over-risks, both of which
        spec §17 forbids. `None` means "cannot be expressed on this broker" and
        must be handled; it is never silently clamped to a tradeable value,
        because clamping is how a rejected size becomes a wrong size.
        """
        value = Decimal(str(volume))
        if value < 0:
            raise ValueError("volume cannot be negative")

        raw = value / self.spec.volume_step
        mode = ROUND_DOWN if rounding == "down" else ROUND_HALF_UP
        steps = int(raw.to_integral_value(rounding=mode))
        snapped = self.steps_to_volume(steps)

        if snapped < self.spec.volume_min or snapped > self.spec.volume_max:
            return None
        return snapped

    def partial_close_volume(
        self,
        current_volume: Decimal | int | str,
        fraction: Decimal | int | str,
    ) -> PartialClose:
        """How much of an open position a partial close may take (spec §17).

        Two broker constraints apply at once, and missing either is a rejected
        order on a live position:

        - the closed amount must be at least ``volume_min``
        - the *remainder* must also be at least ``volume_min`` (a broker will not
          leave a sub-minimum position open), or this is not a partial close

        So the close is capped at ``current - volume_min`` before rounding down.
        The caller decides what to do when this cannot be satisfied — the
        deterministic fallback is a full close or an SL-only move, never a
        silent over-close.
        """
        current = Decimal(str(current_volume))
        frac = Decimal(str(fraction))
        if not (0 < frac < 1):
            raise ValueError("fraction must be strictly between 0 and 1; use a full close otherwise")

        target = current * frac
        headroom = current - self.spec.volume_min
        capped = min(target, headroom)

        if capped < self.spec.volume_min:
            # Either the position is too small to split at all, or honouring the
            # remainder floor would leave nothing meaningful to close.
            verdict = (
                PartialCloseVerdict.REMAINDER_BELOW_MIN
                if target >= self.spec.volume_min
                else PartialCloseVerdict.TOO_SMALL_TO_CLOSE
            )
            return PartialClose(verdict, Decimal(0), current)

        steps = int((capped / self.spec.volume_step).to_integral_value(rounding=ROUND_DOWN))
        volume = self.steps_to_volume(steps)
        if volume < self.spec.volume_min:
            return PartialClose(PartialCloseVerdict.REMAINDER_BELOW_MIN, Decimal(0), current)

        remainder = current - volume
        return PartialClose(PartialCloseVerdict.PARTIAL, volume, remainder)

    # -- spread and the side that closes ------------------------------------

    def spread_pips(self, bid: Decimal | int | str, ask: Decimal | int | str) -> Decimal:
        return self.price_to_pips(Decimal(str(ask)) - Decimal(str(bid)))

    def closing_price(self, side: Side, bid: Decimal | int | str, ask: Decimal | int | str) -> Decimal:
        """The quote that a position of this side is marked and closed against.

        This is the §14 subtlety that must not be mirrored across BUY and SELL:
        a BUY is entered at the ask and closed at the **bid**, so its stop
        triggers on the bid. A SELL is entered at the bid and closed at the
        **ask**. Measuring both from mid, or always from bid, is wrong by one
        spread on one of the two directions — and wrong exactly when the spread
        is wide, which is when the protective move matters most.
        """
        return Decimal(str(bid)) if side is Side.BUY else Decimal(str(ask))

    def validate_stop_distance(
        self,
        side: Side,
        stop_loss: Decimal | int | str,
        bid: Decimal | int | str,
        ask: Decimal | int | str,
    ) -> StopCheck:
        """Is this stop placeable, measured from the side that triggers it?

        Only the conservative direction matters: anything this accepts must
        satisfy the broker's minimum distance. A false rejection costs a missed
        action, which spec §9's asymmetry says is free.

        `stops_level` and `freeze_level` are different constraints with
        different remedies and are reported separately — recomputing further out
        fixes TOO_CLOSE, and only waiting fixes FROZEN.
        """
        trigger = self.closing_price(side, bid, ask)
        sl = Decimal(str(stop_loss))

        if side is Side.BUY and sl >= trigger:
            return StopCheck(StopVerdict.WRONG_SIDE, 0, self.spec.stops_level_points)
        if side is Side.SELL and sl <= trigger:
            return StopCheck(StopVerdict.WRONG_SIDE, 0, self.spec.stops_level_points)

        distance = abs(self.price_to_points(trigger) - self.price_to_points(sl))

        if distance <= self.spec.freeze_level_points:
            return StopCheck(StopVerdict.FROZEN, distance, self.spec.freeze_level_points)
        if distance < self.spec.stops_level_points:
            return StopCheck(StopVerdict.TOO_CLOSE, distance, self.spec.stops_level_points)
        return StopCheck(StopVerdict.OK, distance, self.spec.stops_level_points)

    # -- money --------------------------------------------------------------

    def money_risk(
        self,
        entry: Decimal | int | str,
        stop_loss: Decimal | int | str,
        volume: Decimal | int | str,
    ) -> Decimal:
        """Account-currency loss if this stop is hit, excluding costs.

        Quote-currency instrument (XAUUSD quotes in USD), so this is the price
        distance times the traded units. Commission, swap and stop slippage are
        added by the risk layer from recorded fills, not estimated here.
        """
        distance = abs(Decimal(str(entry)) - Decimal(str(stop_loss)))
        units = Decimal(str(volume)) * self.spec.contract_size
        return distance * units

    def volume_for_risk(
        self,
        risk_budget: Decimal | int | str,
        sl_distance_pips: Decimal | int | str,
    ) -> Decimal | None:
        """Largest tradeable volume whose loss stays inside the budget.

        Provided for completeness and for the monotonicity property test; this
        account uses fixed minimum volume rather than risk-based sizing, because
        risk-based sizing makes a tighter stop buy a *larger* lot and a $1,000
        account has no room for that to be safe.

        Rounds down, so rounding can never push realised risk above the budget.
        """
        pips = Decimal(str(sl_distance_pips))
        if pips <= 0:
            raise ValueError("sl_distance_pips must be positive")
        per_unit = self.pips_to_price(pips) * self.spec.contract_size
        if per_unit <= 0:
            return None
        return self.normalize_volume(Decimal(str(risk_budget)) / per_unit, rounding="down")
