import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings


@dataclass
class LearningProfile:
    confidence_threshold: float
    risk_multiplier: float = 1.0
    last_win_rate: float | None = None
    last_avg_pnl_pct: float | None = None
    samples: int = 0
    updated_at: str | None = None
    last_trade_id: int | None = None


class LearningAgent:
    """Conservative adaptation from completed trades. It never edits source code or removes risk limits."""

    def __init__(self, settings: Settings):
        self.settings = settings
        base = Path(settings.learning_profile_path)
        suffix = base.suffix or ".json"
        self.path = base.with_name(
            f"{base.stem}.{settings.mode}.{settings.symbol}{suffix}"
        )

    def default_profile(self) -> LearningProfile:
        return LearningProfile(
            confidence_threshold=self.settings.min_signal_confidence,
            risk_multiplier=1.0,
        )

    def _learning_enabled_for_mode(self) -> bool:
        if not self.settings.enable_adaptive_learning:
            return False
        if self.settings.mode == "live" and not self.settings.allow_adaptive_live:
            return False
        return True

    def load(self) -> LearningProfile:
        # Disabled learning must have zero runtime influence from any saved profile.
        if not self._learning_enabled_for_mode():
            return self.default_profile()

        if not self.path.exists():
            return self.default_profile()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            profile = LearningProfile(**data)
            profile.confidence_threshold = max(
                self.settings.min_signal_confidence,
                float(profile.confidence_threshold),
            )
            profile.risk_multiplier = min(1.0, max(0.5, float(profile.risk_multiplier)))
            return profile
        except Exception:
            return self.default_profile()

    def update(self, closed_trades: list[dict]) -> LearningProfile:
        if not self._learning_enabled_for_mode():
            return self.default_profile()

        profile = self.load()
        if len(closed_trades) < self.settings.min_trades_for_learning:
            return profile

        latest_trade_id = int(closed_trades[0]["id"])
        if profile.last_trade_id == latest_trade_id:
            return profile

        sample = closed_trades[: min(50, len(closed_trades))]
        wins = sum(1 for t in sample if float(t.get("pnl", 0) or 0) > 0)
        win_rate = wins / len(sample)
        avg_pnl_pct = sum(float(t.get("pnl_pct", 0) or 0) for t in sample) / len(sample)

        threshold = max(
            self.settings.min_signal_confidence,
            float(profile.confidence_threshold),
        )
        risk_multiplier = min(1.0, max(0.5, float(profile.risk_multiplier)))

        if win_rate < 0.45 or avg_pnl_pct <= 0:
            threshold = min(0.95, threshold + 0.03)
            risk_multiplier = max(0.50, risk_multiplier - 0.05)
        elif win_rate > 0.60 and avg_pnl_pct > 0:
            threshold = max(self.settings.min_signal_confidence, threshold - 0.02)
            risk_multiplier = min(1.00, risk_multiplier + 0.05)

        profile = LearningProfile(
            confidence_threshold=round(
                max(self.settings.min_signal_confidence, threshold), 4
            ),
            risk_multiplier=round(risk_multiplier, 4),
            last_win_rate=round(win_rate, 4),
            last_avg_pnl_pct=round(avg_pnl_pct, 6),
            samples=len(sample),
            updated_at=datetime.now(timezone.utc).isoformat(),
            last_trade_id=latest_trade_id,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(profile), indent=2), encoding="utf-8")
        return profile
