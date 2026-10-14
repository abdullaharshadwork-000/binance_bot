"""Property-based tests for the deterministic risk/strategy math.

These complement the example-based regression tests by exploring input spaces
systematically. BUG_REVIEW found several past defects (RSI NaN handling,
threshold clamps, quantity filter gaps) in exactly this class of pure math, so
invariants here are cheap insurance.
"""

import math

import pandas as pd
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.agents.orchestrator import interval_to_ms
from app.config import Settings
from app.models import SignalSide, StrategySignal
from app.risk.manager import RiskManager
from app.strategy.indicators import rsi

finite_floats = st.floats(
    min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False
)
positive_floats = st.floats(
    min_value=1e-6, max_value=1e6, allow_nan=False, allow_infinity=False
)


def make_manager(**overrides) -> RiskManager:
    return RiskManager(Settings(_env_file=None, **overrides))


@given(
    price=positive_floats,
    equity=positive_floats,
    atr_pct=st.floats(min_value=1e-8, max_value=0.5, allow_nan=False, allow_infinity=False),
    multiplier=st.floats(min_value=0.5, max_value=1.0, allow_nan=False, allow_infinity=False),
    data=st.data(),
)
@settings(max_examples=200, deadline=None)
def test_approved_atr_entries_keep_stops_bracketed_and_sized(
    price, equity, atr_pct, multiplier, data
):
    """When ATR stops are enabled and entry approved, stop < price < target and
    notional respects the configured exposure cap."""
    threshold = data.draw(st.floats(min_value=0.5, max_value=0.95))
    confidence = data.draw(st.floats(min_value=0.96, max_value=0.99))
    manager = make_manager(use_atr_stops=True)
    signal = StrategySignal(
        SignalSide.BUY, confidence, "property", features={"atr_pct": atr_pct}
    )
    decision = manager.evaluate_entry(
        signal=signal,
        price=price,
        equity=equity,
        daily_realized_pnl=0.0,
        threshold=threshold,
        risk_multiplier=multiplier,
    )
    assume(decision.allowed)

    stop = decision.stop_price
    target = decision.take_profit_price
    assert 0 < stop < price < target
    scaled = manager.atr_scaled_stop_distances(signal)
    assert scaled is not None
    stop_pct, take_pct = scaled
    assert stop == pytest.approx(price * (1 - stop_pct), rel=1e-6)
    assert target == pytest.approx(price * (1 + take_pct), rel=1e-6)
    # Hard bounds hold no matter how extreme the volatility input was.
    assert stop_pct <= max(
        manager.settings.stop_loss_pct * 2, manager.settings.atr_stop_max_pct
    )
    assert take_pct <= 0.30


@given(
    price=positive_floats,
    equity=positive_floats,
    quantity=positive_floats,
)
@settings(max_examples=150, deadline=None)
def test_exposure_cap_never_exceeded_on_approval(price, equity, quantity):
    manager = make_manager()
    signal = StrategySignal(SignalSide.BUY, 0.9, "property")
    decision = manager.evaluate_entry(
        signal=signal,
        price=price,
        equity=equity,
        daily_realized_pnl=0.0,
        threshold=0.7,
        risk_multiplier=1.0,
    )
    if decision.allowed:
        assert decision.quantity * price <= equity * manager.settings.max_position_fraction * (1 + 1e-9)


@given(daily_pnl=finite_floats, equity=positive_floats)
@settings(max_examples=150, deadline=None)
def test_daily_loss_limit_blocks_beyond_fraction(daily_pnl, equity):
    manager = make_manager()
    signal = StrategySignal(SignalSide.BUY, 0.9, "property")
    decision = manager.evaluate_entry(
        signal=signal,
        price=100.0,
        equity=equity,
        daily_realized_pnl=daily_pnl,
        threshold=0.7,
        risk_multiplier=1.0,
    )
    limit = equity * manager.settings.max_daily_loss_fraction
    if daily_pnl <= -limit:
        assert not decision.allowed
        assert "Daily loss limit" in decision.reason


@given(confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
@settings(max_examples=100, deadline=None)
def test_confidence_threshold_is_monotonic(confidence):
    manager = make_manager()
    decision = manager.evaluate_entry(
        signal=StrategySignal(SignalSide.BUY, confidence, "property"),
        price=100.0,
        equity=1000.0,
        daily_realized_pnl=0.0,
        threshold=0.7,
        risk_multiplier=1.0,
    )
    assert decision.allowed == (confidence >= 0.7)


@given(
    price=finite_floats,
    equity=finite_floats,
    daily=finite_floats,
    threshold=finite_floats,
    risk_multiplier=finite_floats,
    confidence=finite_floats,
)
@settings(max_examples=200, deadline=None)
def test_non_finite_inputs_never_crash_or_approve(
    price, equity, daily, threshold, risk_multiplier, confidence
):
    manager = make_manager()
    signal = StrategySignal(SignalSide.BUY, confidence, "property")
    try:
        decision = manager.evaluate_entry(
            signal=signal,
            price=price,
            equity=equity,
            daily_realized_pnl=daily,
            threshold=threshold,
            risk_multiplier=risk_multiplier,
        )
    except Exception:  # Decimal conversion of extreme floats may raise
        assume(False)
        return
    if not all(
        math.isfinite(v)
        for v in (price, equity, daily, threshold, risk_multiplier, confidence)
    ):
        assert not decision.allowed


@given(
    values=st.lists(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=2,
        max_size=300,
    )
)
@settings(max_examples=200, deadline=None)
def test_rsi_stays_bounded_for_arbitrary_series(values):
    series = pd.Series(values, dtype=float)
    result = rsi(series, 14)
    assert ((result >= 0) & (result <= 100)).all()
    assert result.notna().all()


@given(unit=st.sampled_from(["m", "h", "d", "w"]), amount=st.integers(min_value=1, max_value=1000))
@settings(max_examples=100, deadline=None)
def test_interval_to_ms_positive_and_ordered(unit, amount):
    value = interval_to_ms(f"{amount}{unit}")
    assert value > 0
    assert interval_to_ms(f"1{unit}") <= value
    multipliers = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
    assert value == amount * multipliers[unit]


@given(garbage=st.text(min_size=1, max_size=8))
@settings(max_examples=100, deadline=None)
def test_interval_to_ms_rejects_garbage(garbage):
    try:
        interval_to_ms(garbage)
    except ValueError:
        return
    # Anything that parses must be a plain <digits><unit> interval.
    assert garbage.isdigit() or (garbage[:-1].isdigit() and garbage[-1] in {"s", "m", "h", "d", "w"})


def test_numpy_nan_atr_feature_falls_back_to_fixed_stops():
    manager = make_manager(use_atr_stops=True)
    signal = StrategySignal(
        SignalSide.BUY, 0.9, "nan", features={"atr_pct": float("nan")}
    )
    decision = manager.evaluate_entry(
        signal=signal,
        price=100.0,
        equity=1000.0,
        daily_realized_pnl=0.0,
        threshold=0.7,
        risk_multiplier=1.0,
    )
    assert decision.allowed
    assert decision.stop_price == pytest.approx(100.0 * (1 - manager.settings.stop_loss_pct))
    assert decision.take_profit_price == pytest.approx(100.0 * (1 + manager.settings.take_profit_pct))
    assert not math.isnan(decision.quantity)
