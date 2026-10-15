import math
from decimal import Decimal

from app.config import Settings
from app.models import RiskDecision, SignalSide, StrategySignal


class RiskManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    def atr_scaled_stop_distances(self, signal: StrategySignal) -> tuple[float, float] | None:
        """Return (stop_pct, take_profit_pct) scaled from the signal candle ATR.

        Distances expand in volatile markets and tighten in quiet ones, bounded
        around the configured fixed percentages. The configured reward-to-risk
        ratio is preserved. Returns None (use fixed distances) when ATR stops
        are disabled or the signal carries no usable ATR feature.
        """
        if not self.settings.use_atr_stops:
            return None
        raw = signal.features.get("atr_pct") if signal.features else None
        if raw is None:
            return None
        try:
            atr_pct = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(atr_pct) or atr_pct <= 0:
            return None

        fixed_stop = self.settings.stop_loss_pct
        stop_pct = self.settings.atr_stop_multiplier * atr_pct
        # Clamp around the configured fixed stop: never tighter than half or
        # wider than double the configured distance, plus absolute bounds.
        stop_pct = min(
            max(stop_pct, fixed_stop / 2, self.settings.atr_stop_min_pct),
            fixed_stop * 2,
            self.settings.atr_stop_max_pct,
        )
        # Preserve the configured reward-to-risk ratio; respect the hard cap.
        take_pct = min(stop_pct * (self.settings.take_profit_pct / fixed_stop), 0.30)
        return stop_pct, take_pct


    def evaluate_entry(
        self,
        *,
        signal: StrategySignal,
        price: float,
        equity: float,
        daily_realized_pnl: float,
        threshold: float,
        risk_multiplier: float,
    ) -> RiskDecision:
        if not all(math.isfinite(value) for value in (
            price, equity, daily_realized_pnl, threshold, risk_multiplier, signal.confidence
        )):
            return RiskDecision(False, "Non-finite market or risk input")
        if signal.side != SignalSide.BUY:
            return RiskDecision(False, "No buy signal")
        if signal.confidence < threshold:
            return RiskDecision(False, f"Confidence {signal.confidence:.3f} below threshold {threshold:.3f}")
        if equity <= 0 or price <= 0:
            return RiskDecision(False, "Equity or market price is zero")

        price_d = Decimal(str(price))
        equity_d = Decimal(str(equity))
        daily_pnl_d = Decimal(str(daily_realized_pnl))
        daily_loss_fraction = Decimal(str(self.settings.max_daily_loss_fraction))

        max_daily_loss = equity_d * daily_loss_fraction
        if daily_pnl_d <= -max_daily_loss:
            return RiskDecision(False, "Daily loss limit reached")

        stop_pct = Decimal(str(self.settings.stop_loss_pct))
        take_pct = Decimal(str(self.settings.take_profit_pct))
        scaled = self.atr_scaled_stop_distances(signal)
        if scaled is not None:
            stop_pct = Decimal(str(scaled[0]))
            take_pct = Decimal(str(scaled[1]))
        risk_pct = Decimal(str(self.settings.risk_per_trade))
        exposure_pct = Decimal(str(self.settings.max_position_fraction))
        multiplier = Decimal(str(max(0.5, min(risk_multiplier, 1.0))))

        # Budget for both commissions and adverse execution, not just the stop.
        fee = Decimal(str(self.settings.trading_fee_bps)) / Decimal("10000")
        slippage_bps = self.settings.paper_slippage_bps if self.settings.mode == "paper" else self.settings.max_entry_slippage_bps
        slippage = Decimal(str(slippage_bps)) / Decimal("10000")
        stop_distance = price_d * (stop_pct + 2 * fee + 2 * slippage)
        risk_budget = equity_d * risk_pct * multiplier
        qty_by_risk = risk_budget / stop_distance
        qty_by_exposure = (equity_d * exposure_pct * multiplier) / (price_d * (1 + slippage) * (1 + fee))
        quantity = min(qty_by_risk, qty_by_exposure)

        return RiskDecision(
            True,
            "Approved by deterministic risk engine",
            quantity=float(max(quantity, Decimal("0"))),
            stop_price=float(price_d * (Decimal("1") - stop_pct)),
            take_profit_price=float(price_d * (Decimal("1") + take_pct)),
        )
