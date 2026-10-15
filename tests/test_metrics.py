"""Tests for the Prometheus text-format metrics renderer."""

from app.metrics import render_metrics


def make_state(**overrides):
    state = {
        "symbol": "BTCUSDT",
        "running": True,
        "cycles_total": 42,
        "cycle_errors_total": 3,
        "last_cycle_seconds": 0.12,
        "price_age_seconds": 0.9,
        "feed_stale": False,
        "open_position": True,
    }
    state.update(overrides)
    return state


def test_renders_prometheus_exposition_format():
    body = render_metrics(
        [make_state()],
        {"mode": "paper", "equity": 1000.5, "daily_realized_pnl": -2.0, "open_positions": 1},
    )
    assert "# TYPE binancebot_engine_running gauge" in body
    assert 'binancebot_engine_running{symbol="BTCUSDT"} 1' in body
    assert 'binancebot_cycles_total{symbol="BTCUSDT"} 42' in body
    assert 'binancebot_cycle_errors_total{symbol="BTCUSDT"} 3' in body
    assert 'binancebot_last_cycle_seconds{symbol="BTCUSDT"} 0.12' in body
    assert 'binancebot_price_age_seconds{symbol="BTCUSDT"} 0.9' in body
    assert 'binancebot_feed_stale{symbol="BTCUSDT"} 0' in body
    assert 'binancebot_open_position{symbol="BTCUSDT"} 1' in body
    assert 'binancebot_equity{mode="paper"} 1000.5' in body
    assert "binancebot_daily_realized_pnl -2.0" in body
    assert "binancebot_open_positions 1" in body
    assert body.endswith("\n")


def test_missing_values_are_omitted_not_zero():
    body = render_metrics(
        [make_state(last_cycle_seconds=None, price_age_seconds=None, feed_stale=True)],
        {"mode": "testnet", "equity": None, "daily_realized_pnl": None, "open_positions": 0},
    )
    sample_lines = [
        line for line in body.splitlines()
        if line and not line.startswith("#")
    ]
    # HELP/TYPE metadata may exist for absent values; sample lines may not.
    assert not any(line.startswith("binancebot_last_cycle_seconds") for line in sample_lines)
    assert not any(line.startswith("binancebot_price_age_seconds") for line in sample_lines)
    assert 'binancebot_feed_stale{symbol="BTCUSDT"} 1' in body
    assert not any(line.startswith("binancebot_equity") for line in sample_lines)
    assert not any(line.startswith("binancebot_daily_realized_pnl ") for line in sample_lines)
    assert "binancebot_open_positions 0" in body


def test_labels_are_escaped():
    body = render_metrics(
        [make_state(symbol='ETH"USDT\\')],
        {"mode": "paper", "equity": None, "daily_realized_pnl": None, "open_positions": 0},
    )
    assert 'symbol="ETH\\"USDT\\\\"' in body


def test_multiple_symbols_render_independently():
    body = render_metrics(
        [
            make_state(symbol="BTCUSDT", cycles_total=1),
            make_state(symbol="ETHUSDT", running=False, cycles_total=2, open_position=False),
        ],
        {"mode": "paper", "equity": 5.0, "daily_realized_pnl": None, "open_positions": 1},
    )
    assert 'binancebot_engine_running{symbol="BTCUSDT"} 1' in body
    assert 'binancebot_engine_running{symbol="ETHUSDT"} 0' in body
    assert 'binancebot_cycles_total{symbol="BTCUSDT"} 1' in body
    assert 'binancebot_cycles_total{symbol="ETHUSDT"} 2' in body


def test_empty_state_renders_portfolio_only():
    body = render_metrics([], {"mode": "paper", "equity": None, "daily_realized_pnl": None, "open_positions": 0})
    assert "binancebot_engine_running" not in body
    assert "binancebot_open_positions 0" in body
