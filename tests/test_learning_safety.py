from app.agents.learning import LearningAgent
from app.config import Settings


def make_trades(count: int, pnl: float = -1.0, pnl_pct: float = -0.01):
    return [
        {"id": count - i, "pnl": pnl, "pnl_pct": pnl_pct}
        for i in range(count)
    ]


def test_disabled_learning_ignores_saved_profile(tmp_path):
    settings = Settings(
        _env_file=None,
        enable_adaptive_learning=False,
        min_signal_confidence=0.90,
        learning_profile_path=str(tmp_path / "learning.json"),
    )
    agent = LearningAgent(settings)
    agent.path.parent.mkdir(parents=True, exist_ok=True)
    agent.path.write_text(
        '{"confidence_threshold": 0.60, "risk_multiplier": 0.5}',
        encoding="utf-8",
    )

    profile = agent.load()

    assert profile.confidence_threshold == 0.90
    assert profile.risk_multiplier == 1.0


def test_learning_never_drops_below_configured_floor(tmp_path):
    settings = Settings(
        _env_file=None,
        enable_adaptive_learning=True,
        min_signal_confidence=0.90,
        min_trades_for_learning=10,
        learning_profile_path=str(tmp_path / "learning.json"),
    )
    agent = LearningAgent(settings)

    losing = make_trades(10, pnl=-1.0, pnl_pct=-0.01)
    profile = agent.update(losing)

    assert profile.confidence_threshold >= 0.90


def test_learning_threshold_above_200_trades_can_activate(tmp_path):
    settings = Settings(
        _env_file=None,
        enable_adaptive_learning=True,
        min_signal_confidence=0.70,
        min_trades_for_learning=250,
        learning_profile_path=str(tmp_path / "learning.json"),
    )
    agent = LearningAgent(settings)

    trades = make_trades(250, pnl=1.0, pnl_pct=0.01)
    profile = agent.update(trades)

    assert profile.last_trade_id == 250
    assert profile.samples == 50


def test_learning_profiles_are_namespaced_by_mode_and_symbol(tmp_path):
    paper = Settings(
        _env_file=None,
        mode="paper",
        symbol="BTCUSDT",
        learning_profile_path=str(tmp_path / "learning.json"),
    )
    other = Settings(
        _env_file=None,
        mode="paper",
        symbol="ETHUSDT",
        base_asset="ETH",
        learning_profile_path=str(tmp_path / "learning.json"),
    )

    assert LearningAgent(paper).path != LearningAgent(other).path
