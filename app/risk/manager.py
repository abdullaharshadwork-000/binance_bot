from decimal import Decimal

from app.config import Settings
from app.models import RiskDecision, SignalSide, StrategySignal


class RiskManager:
    def __init__(self, settings: Settings):
        self.settings = settings

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
        risk_pct = Decimal(str(self.settings.risk_per_trade))
        exposure_pct = Decimal(str(self.settings.max_position_fraction))
        multiplier = Decimal(str(max(0.5, min(risk_multiplier, 1.0))))

        stop_distance = price_d * stop_pct
        risk_budget = equity_d * risk_pct * multiplier
        qty_by_risk = risk_budget / stop_distance
        qty_by_exposure = (equity_d * exposure_pct) / price_d
        quantity = min(qty_by_risk, qty_by_exposure)

        return RiskDecision(
            True,
            "Approved by deterministic risk engine",
            quantity=float(max(quantity, Decimal("0"))),
            stop_price=float(price_d * (Decimal("1") - stop_pct)),
            take_profit_price=float(price_d * (Decimal("1") + take_pct)),
        )
