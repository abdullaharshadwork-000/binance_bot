import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TradingDB:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    entry_fee REAL NOT NULL DEFAULT 0,
                    exit_fee REAL NOT NULL DEFAULT 0,
                    pnl REAL,
                    pnl_pct REAL,
                    status TEXT NOT NULL,
                    entry_reason TEXT,
                    exit_reason TEXT,
                    stop_price REAL,
                    take_profit_price REAL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def open_trade(
        self,
        *,
        mode: str,
        symbol: str,
        quantity: float,
        entry_price: float,
        entry_fee: float,
        reason: str,
        stop_price: float,
        take_profit_price: float,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO trades(
                    mode, symbol, quantity, entry_price, entry_fee, status,
                    entry_reason, stop_price, take_profit_price, opened_at
                ) VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)
                """,
                (mode, symbol, quantity, entry_price, entry_fee, reason, stop_price, take_profit_price, now),
            )
            return int(cur.lastrowid)

    def close_trade(self, trade_id: int, exit_price: float, exit_fee: float, reason: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM trades WHERE id=? AND status='OPEN'", (trade_id,)).fetchone()
            if not row:
                raise ValueError("Open trade not found")
            gross = (exit_price - row["entry_price"]) * row["quantity"]
            pnl = gross - row["entry_fee"] - exit_fee
            cost = row["entry_price"] * row["quantity"]
            pnl_pct = pnl / cost if cost > 0 else 0.0
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE trades
                SET exit_price=?, exit_fee=?, pnl=?, pnl_pct=?, status='CLOSED', exit_reason=?, closed_at=?
                WHERE id=?
                """,
                (exit_price, exit_fee, pnl, pnl_pct, reason, now, trade_id),
            )
            return {"id": trade_id, "pnl": pnl, "pnl_pct": pnl_pct}

    def get_open_trade(self, symbol: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM trades WHERE symbol=? AND status='OPEN' ORDER BY id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            return dict(row) if row else None

    def list_trades(self, limit: int = 100) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def closed_trades(self, limit: int = 200) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status='CLOSED' ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def realized_pnl_today(self) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl),0) AS total FROM trades WHERE status='CLOSED' AND substr(closed_at,1,10)=?",
                (today,),
            ).fetchone()
            return float(row["total"] or 0.0)

    def performance_summary(self) -> dict:
        """Return simple realized-trade analytics for the dashboard."""
        trades = list(reversed(self.closed_trades(limit=500)))
        if not trades:
            return {
                "closed_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": None,
                "total_pnl": 0.0,
                "avg_pnl": None,
                "avg_pnl_pct": None,
                "best_trade": None,
                "worst_trade": None,
                "profit_factor": None,
                "max_drawdown": 0.0,
                "equity_curve": [],
            }

        pnls = [float(t.get("pnl") or 0.0) for t in trades]
        pnl_pcts = [float(t.get("pnl_pct") or 0.0) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))

        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0
        curve = []
        for trade, pnl in zip(trades, pnls):
            cumulative += pnl
            peak = max(peak, cumulative)
            drawdown = peak - cumulative
            max_drawdown = max(max_drawdown, drawdown)
            curve.append({
                "id": int(trade["id"]),
                "closed_at": trade.get("closed_at"),
                "cumulative_pnl": round(cumulative, 8),
            })

        return {
            "closed_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades),
            "total_pnl": sum(pnls),
            "avg_pnl": sum(pnls) / len(trades),
            "avg_pnl_pct": sum(pnl_pcts) / len(trades),
            "best_trade": max(pnls),
            "worst_trade": min(pnls),
            "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,
            "max_drawdown": max_drawdown,
            "equity_curve": curve[-100:],
        }

