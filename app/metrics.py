"""Prometheus text-format metrics for the trading engine.

Rendered from in-memory engine state on scrape; no external dependencies and
no mutable global registry. Counter/gauge names use the ``binancebot_``
prefix. The endpoint shares the dashboard's exposure model: it is intended
for localhost/private-network monitoring, not the public internet.
"""

from __future__ import annotations

from typing import Any


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{key}="{_escape_label(str(value))}"' for key, value in labels.items())
    return "{" + inner + "}"


def _metric(
    lines: list[str],
    name: str,
    metric_type: str,
    help_text: str,
    value: float | int | None,
    labels: dict[str, str] | None = None,
) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {metric_type}")
    if value is not None:
        lines.append(f"{name}{_labels(labels or {})} {value}")


def render_metrics(bots_state: list[dict[str, Any]], portfolio: dict[str, Any]) -> str:
    """Render metrics from per-symbol engine state and a portfolio snapshot.

    Each entry in ``bots_state`` needs: symbol, running, cycles_total,
    cycle_errors_total, last_cycle_seconds, price_age_seconds, feed_stale,
    open_position (bool).
    """
    lines: list[str] = []

    for state in bots_state:
        symbol_labels = {"symbol": state["symbol"]}
        _metric(
            lines,
            "binancebot_engine_running",
            "gauge",
            "1 when the per-symbol engine loop is running.",
            1 if state.get("running") else 0,
            symbol_labels,
        )
        _metric(
            lines,
            "binancebot_cycles_total",
            "counter",
            "Completed market cycles per symbol.",
            state.get("cycles_total"),
            symbol_labels,
        )
        _metric(
            lines,
            "binancebot_cycle_errors_total",
            "counter",
            "Market cycles that ended in an unexpected error per symbol.",
            state.get("cycle_errors_total"),
            symbol_labels,
        )
        _metric(
            lines,
            "binancebot_last_cycle_seconds",
            "gauge",
            "Duration of the most recent completed market cycle.",
            state.get("last_cycle_seconds"),
            symbol_labels,
        )
        _metric(
            lines,
            "binancebot_price_age_seconds",
            "gauge",
            "Age of the freshest market price known to the engine.",
            state.get("price_age_seconds"),
            symbol_labels,
        )
        _metric(
            lines,
            "binancebot_feed_stale",
            "gauge",
            "1 when the market feed is older than the configured stale window.",
            1 if state.get("feed_stale") else 0,
            symbol_labels,
        )
        _metric(
            lines,
            "binancebot_open_position",
            "gauge",
            "1 when the engine currently holds an open position.",
            1 if state.get("open_position") else 0,
            symbol_labels,
        )

    _metric(
        lines,
        "binancebot_equity",
        "gauge",
        "Estimated portfolio equity in the quote asset; None while unavailable.",
        portfolio.get("equity"),
        {"mode": portfolio.get("mode", "")},
    )
    _metric(
        lines,
        "binancebot_daily_realized_pnl",
        "gauge",
        "Realized PnL today across configured symbols in the quote asset.",
        portfolio.get("daily_realized_pnl"),
    )
    _metric(
        lines,
        "binancebot_open_positions",
        "gauge",
        "Number of currently open positions across configured symbols.",
        portfolio.get("open_positions"),
    )

    return "\n".join(lines) + ("\n" if lines else "")
