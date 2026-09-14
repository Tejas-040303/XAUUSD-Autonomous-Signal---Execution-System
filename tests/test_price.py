"""Price, pip and volume arithmetic.

The property tests here are the ones that matter: spec §17 forbids silently
over-closing or increasing risk, and those are properties over all inputs, not
facts about one worked example. §16's 2.00-lot example divides cleanly and so
passes trivially — the bugs live at 0.03 lots with a 0.02 step.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from xauusd.price import (
    PartialCloseVerdict,
    PriceUtils,
    Side,
    StopVerdict,
    SymbolSpec,
)

# ---------------------------------------------------------------------------
# SymbolSpec invariants
# ---------------------------------------------------------------------------


def test_pip_size_must_be_whole_points():
    """A pip that is not a whole number of points means one value is wrong."""
    with pytest.raises(ValueError, match="whole multiple of point"):
        SymbolSpec(
            name="X", digits=2, point=Decimal("0.03"), pip_size=Decimal("0.10"),
            contract_size=Decimal("100"), volume_min=Decimal("0.01"),
            volume_max=Decimal("50"), volume_step=Decimal("0.01"),
            stops_level_points=0, freeze_level_points=0,
        )


def test_volume_min_must_sit_on_the_step_grid():
    with pytest.raises(ValueError, match="not a multiple of volume_step"):
        SymbolSpec(
            name="X", digits=2, point=Decimal("0.01"), pip_size=Decimal("0.10"),
            contract_size=Decimal("100"), volume_min=Decimal("0.015"),
            volume_max=Decimal("50"), volume_step=Decimal("0.01"),
            stops_level_points=0, freeze_level_points=0,
        )


def test_no_pip_default_exists():
    """pip_size is required. There is no convention to fall back on."""
    with pytest.raises(TypeError):
        SymbolSpec(  # type: ignore[call-arg]
            name="X", digits=2, point=Decimal("0.01"),
            contract_size=Decimal("100"), volume_min=Decimal("0.01"),
            volume_max=Decimal("50"), volume_step=Decimal("0.01"),
            stops_level_points=0, freeze_level_points=0,
        )


# ---------------------------------------------------------------------------
# pip <-> price round trips
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(pips=st.decimals(min_value=1, max_value=500, places=1))
def test_pip_price_round_trip_within_one_point(utils: PriceUtils, pips: Decimal):
    """Round-trip to within half a point, not exact equality.

    Asserting `==` would be wrong for a quantised price and would get papered
    over with an arbitrary epsilon; the honest tolerance is the broker's own
    grid resolution.
    """
    back = utils.price_to_pips(utils.pips_to_price(pips))
    tolerance = utils.points_to_pips(1)
    assert abs(back - pips) <= tolerance


@pytest.mark.property
@given(price=st.decimals(min_value=1000, max_value=5000, places=4))
def test_normalize_price_is_on_the_grid_and_close(utils: PriceUtils, price: Decimal):
    snapped = utils.normalize_price(price)
    # exactly representable on the point grid
    assert utils.points_to_price(utils.price_to_points(snapped)) == snapped
    # and never moved more than half a point
    assert abs(snapped - price) <= utils.spec.point / 2


def test_normalize_price_is_idempotent(utils: PriceUtils):
    once = utils.normalize_price("2650.127")
    assert utils.normalize_price(once) == once


# ---------------------------------------------------------------------------
# volume grid
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(volume=st.decimals(min_value="0.01", max_value=50, places=4))
def test_normalized_volume_is_on_the_step_grid(utils: PriceUtils, volume: Decimal):
    result = utils.normalize_volume(volume)
    assume(result is not None)
    steps = result / utils.spec.volume_step
    assert steps == steps.to_integral_value(), f"{result} is off the step grid"
    assert utils.spec.volume_min <= result <= utils.spec.volume_max


@pytest.mark.property
@given(volume=st.decimals(min_value="0.01", max_value=50, places=4))
def test_normalize_volume_never_rounds_up(utils: PriceUtils, volume: Decimal):
    """Rounding up an entry over-risks and rounding up a close over-closes."""
    result = utils.normalize_volume(volume, rounding="down")
    assume(result is not None)
    assert result <= volume


@pytest.mark.property
@given(
    a=st.decimals(min_value="0.01", max_value=50, places=2),
    b=st.decimals(min_value="0.01", max_value=50, places=2),
)
def test_normalize_volume_is_monotonic(utils: PriceUtils, a: Decimal, b: Decimal):
    """Non-monotone rounding is the classic source of 'closed more than I held'."""
    lo, hi = sorted((a, b))
    r_lo, r_hi = utils.normalize_volume(lo), utils.normalize_volume(hi)
    assume(r_lo is not None and r_hi is not None)
    assert r_lo <= r_hi


def test_normalize_volume_returns_none_below_minimum(utils: PriceUtils):
    """None means 'cannot be traded' and must be handled — never clamped up."""
    assert utils.normalize_volume("0.004") is None


def test_normalize_volume_rejects_negative(utils: PriceUtils):
    with pytest.raises(ValueError):
        utils.normalize_volume("-0.01")


# ---------------------------------------------------------------------------
# partial close (spec §17)
# ---------------------------------------------------------------------------


def test_partial_close_worked_example_from_spec_16(utils: PriceUtils):
    """§16's example: 2.00 lots -> TP1 1.00 -> TP2 0.50 -> final 0.50."""
    tp1 = utils.partial_close_volume("2.00", "0.5")
    assert tp1.ok and tp1.volume == Decimal("1.00") and tp1.remainder == Decimal("1.00")

    tp2 = utils.partial_close_volume(tp1.remainder, "0.5")
    assert tp2.ok and tp2.volume == Decimal("0.50") and tp2.remainder == Decimal("0.50")


def test_partial_close_cannot_split_the_minimum_lot(utils: PriceUtils):
    """At the minimum size the ladder is inert — which is what P7 starts at.

    50% of 0.01 is 0.005, below volume_min, so no partial close exists. The
    caller must fall back (SL move only, or full close), never over-close.
    """
    result = utils.partial_close_volume("0.01", "0.5")
    assert not result.ok
    assert result.verdict is PartialCloseVerdict.TOO_SMALL_TO_CLOSE
    assert result.volume == 0
    assert result.remainder == Decimal("0.01")


def test_partial_close_respects_the_remainder_floor():
    """A close that would leave a sub-minimum position is not a partial close."""
    spec = SymbolSpec(
        name="X", digits=2, point=Decimal("0.01"), pip_size=Decimal("0.10"),
        contract_size=Decimal("100"), volume_min=Decimal("0.10"),
        volume_max=Decimal("50"), volume_step=Decimal("0.01"),
        stops_level_points=0, freeze_level_points=0,
    )
    utils = PriceUtils(spec)
    # 0.15 lots, want 50% = 0.075 -> below min. Capped at 0.15-0.10 = 0.05, still below min.
    result = utils.partial_close_volume("0.15", "0.5")
    assert not result.ok
    assert result.remainder == Decimal("0.15")


@pytest.mark.property
@given(
    volume=st.decimals(min_value="0.01", max_value=10, places=2),
    fraction=st.decimals(min_value="0.1", max_value="0.9", places=1),
)
def test_partial_close_never_over_closes(utils: PriceUtils, volume: Decimal, fraction: Decimal):
    """The §17 safety property, stated as a property rather than an example."""
    result = utils.partial_close_volume(volume, fraction)
    assert result.volume <= volume, "over-closed"
    assert result.volume >= 0
    assert result.remainder == volume - result.volume
    if result.ok:
        # both legs must be independently tradeable
        assert result.volume >= utils.spec.volume_min
        assert result.remainder >= utils.spec.volume_min


@pytest.mark.property
@given(volume=st.decimals(min_value="0.02", max_value=10, places=2))
def test_tp_ladder_conserves_volume(utils: PriceUtils, volume: Decimal):
    """Across a full 3-leg ladder, closed legs plus remainder equal the original.

    This is what catches the "TP2 is 50% of remaining, not 50% of original"
    bug class at every volume, rather than only at the one that divides evenly.
    """
    remaining = volume
    closed = Decimal(0)
    for _ in range(3):
        leg = utils.partial_close_volume(remaining, "0.5")
        if not leg.ok:
            break
        closed += leg.volume
        remaining = leg.remainder
    assert closed + remaining == volume


# ---------------------------------------------------------------------------
# spread and the side that closes (spec §14)
# ---------------------------------------------------------------------------


def test_buy_closes_on_bid_and_sell_on_ask(utils: PriceUtils):
    """The §14 asymmetry. Mirroring one formula across both sides is the bug."""
    bid, ask = Decimal("2650.00"), Decimal("2650.30")
    assert utils.closing_price(Side.BUY, bid, ask) == bid
    assert utils.closing_price(Side.SELL, bid, ask) == ask


def test_spread_is_measured_in_pips(utils: PriceUtils):
    spread = utils.spread_pips("2650.00", "2650.30")
    assert spread == utils.price_to_pips(Decimal("0.30"))


# ---------------------------------------------------------------------------
# stop distance
# ---------------------------------------------------------------------------


def test_stop_on_the_wrong_side_is_rejected(broker_spec: SymbolSpec):
    utils = PriceUtils(broker_spec)
    bid, ask = Decimal("2650.00"), Decimal("2650.30")
    # A BUY stop must sit below the bid.
    assert utils.validate_stop_distance(Side.BUY, "2651.00", bid, ask).verdict is StopVerdict.WRONG_SIDE
    # A SELL stop must sit above the ask.
    assert utils.validate_stop_distance(Side.SELL, "2649.00", bid, ask).verdict is StopVerdict.WRONG_SIDE


def test_freeze_and_stops_level_are_reported_separately(broker_spec: SymbolSpec):
    """Different constraints, different remedies: recompute vs wait."""
    utils = PriceUtils(broker_spec)
    bid, ask = Decimal("2650.00"), Decimal("2650.30")

    # 10 points below bid: inside the 20-point freeze band.
    frozen = utils.validate_stop_distance(Side.BUY, "2649.90", bid, ask)
    assert frozen.verdict is StopVerdict.FROZEN

    # 30 points below bid: clear of freeze, inside the 50-point stops level.
    too_close = utils.validate_stop_distance(Side.BUY, "2649.70", bid, ask)
    assert too_close.verdict is StopVerdict.TOO_CLOSE

    # 100 points below bid: placeable.
    ok = utils.validate_stop_distance(Side.BUY, "2649.00", bid, ask)
    assert ok.ok and ok.distance_points == 100


def test_buy_stop_distance_is_measured_from_bid_not_ask(broker_spec: SymbolSpec):
    """Measuring from the wrong quote is off by one spread — and wrong exactly
    when the spread is wide, which is when it matters."""
    utils = PriceUtils(broker_spec)
    bid, ask = Decimal("2650.00"), Decimal("2651.00")  # 100-point spread
    check = utils.validate_stop_distance(Side.BUY, "2649.00", bid, ask)
    assert check.distance_points == 100  # from bid, not 200 from ask


@pytest.mark.property
@given(distance_points=st.integers(min_value=1, max_value=5000))
def test_accepted_stops_always_clear_the_broker_minimum(
    broker_spec: SymbolSpec, distance_points: int
):
    """One-sidedly conservative: anything accepted satisfies the real constraint.

    False rejections are free (spec §9); false acceptances are a rejected order
    on a live position.
    """
    utils = PriceUtils(broker_spec)
    bid, ask = Decimal("2650.00"), Decimal("2650.30")
    sl = utils.points_to_price(utils.price_to_points(bid) - distance_points)
    check = utils.validate_stop_distance(Side.BUY, sl, bid, ask)
    if check.ok:
        assert check.distance_points >= broker_spec.stops_level_points


# ---------------------------------------------------------------------------
# money
# ---------------------------------------------------------------------------


def test_money_risk_matches_the_contract_size(utils: PriceUtils):
    """0.01 lots of XAUUSD is 1 oz, so a $7.00 move is $7.00."""
    risk = utils.money_risk(entry="2650.00", stop_loss="2643.00", volume="0.01")
    assert risk == Decimal("7.00")


def test_pip_convention_decides_whether_the_account_can_trade(utils: PriceUtils):
    """The consequence that makes the pip definition a blocker.

    70 pips on the smallest placeable lot, against a $50 daily loss limit:
      pip=$0.01 -> $0.70   pip=$0.10 -> $7.00   pip=$1.00 -> $70.00
    Only the middle one leaves room for four losses in a day.
    """
    sl_distance = utils.pips_to_price(70)
    risk = utils.money_risk("2650.00", Decimal("2650.00") - sl_distance, "0.01")
    expected = {
        Decimal("0.01"): Decimal("0.70"),
        Decimal("0.10"): Decimal("7.00"),
        Decimal("1.00"): Decimal("70.00"),
    }[utils.spec.pip_size]
    assert risk == expected
    # And the one that cannot work: a single minimum trade exceeds the day's budget.
    if utils.spec.pip_size == Decimal("1.00"):
        assert risk > Decimal("50")


@pytest.mark.property
@settings(max_examples=50)
@given(sl_pips=st.decimals(min_value=1, max_value=200, places=1))
def test_risk_based_volume_is_non_increasing_in_stop_distance(
    utils: PriceUtils, sl_pips: Decimal
):
    """A wider stop must never buy a larger lot.

    A divide/multiply inversion here produces maximum size on the widest stop —
    the worst possible failure — and no worked example would catch it.
    """
    budget = Decimal("12.50")
    wider = sl_pips + 10
    v_narrow = utils.volume_for_risk(budget, sl_pips)
    v_wide = utils.volume_for_risk(budget, wider)
    if v_narrow is not None and v_wide is not None:
        assert v_wide <= v_narrow


@pytest.mark.property
@settings(max_examples=50)
@given(sl_pips=st.decimals(min_value=1, max_value=200, places=1))
def test_risk_based_volume_either_fits_the_budget_or_is_refused(
    utils: PriceUtils, sl_pips: Decimal
):
    """Both branches, asserted — no filtering.

    Either a tradeable volume exists and step rounding kept its realised risk
    inside the budget, or none exists because the *minimum* lot already exceeds
    it. Asserting the second branch rather than discarding it is what makes the
    pip-convention consequence visible: at $1.00/pip the minimum 0.01 lot risks
    $1 per pip, so a $12.50 budget refuses any stop wider than 12 pips — which
    is every real signal.
    """
    budget = Decimal("12.50")
    entry = Decimal("2650.00")
    sl = entry - utils.pips_to_price(sl_pips)
    volume = utils.volume_for_risk(budget, sl_pips)

    if volume is not None:
        assert utils.money_risk(entry, sl, volume) <= budget
    else:
        min_lot_risk = utils.money_risk(entry, sl, utils.spec.volume_min)
        assert min_lot_risk > budget, (
            "volume was refused, but the minimum lot would have fitted the budget"
        )
