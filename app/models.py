from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SignalSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class StrategySignal:
    side: SignalSide
    confidence: float
    reason: str
    features: dict[str, float] = field(default_factory=dict)


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    quantity: float = 0.0
    stop_price: float | None = None
    take_profit_price: float | None = None


@dataclass
class ExecutionResult:
    action: str
    success: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)
