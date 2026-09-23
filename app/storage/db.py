import json
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
        except Exception:
            conn.rollback()
            raise
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

                CREATE INDEX IF NOT EXISTS idx_trades_mode_symbol_status
                ON trades(mode, symbol, status);

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    client_order_id TEXT NOT NULL UNIQUE,
                    binance_order_id TEXT,
                    side TEXT NOT NULL,
                    requested_quantity REAL NOT NULL,
                    executed_quantity REAL NOT NULL DEFAULT 0,
                    average_fill_price REAL,
                    status TEXT NOT NULL,
                    commission_quote REAL NOT NULL DEFAULT 0,
                    commission_details TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_orders_mode_symbol_status
                ON orders(mode, symbol, status);

                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _set_state_conn(conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute(
            "INSERT INTO state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self.connection() as conn:
            self._set_state_conn(conn, key, value)

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
        state_updates: dict[str, Any] | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            if state_updates:
                for key, value in state_updates.items():
                    self._set_state_conn(conn, key, value)
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

    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        exit_fee: float,
        reason: str,
        *,
        executed_quantity: float | None = None,
        state_updates: dict[str, Any] | None = None,
    ) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM trades WHERE id=? AND status='OPEN'",
                (trade_id,),
            ).fetchone()
            if not row:
                raise ValueError("Open trade not found")

            position_qty = float(row["quantity"])
            sold_qty = position_qty if executed_quantity is None else min(position_qty, max(0.0, float(executed_quantity)))
            if sold_qty <= 0:
                raise ValueError("Executed quantity must be positive")

            ratio = sold_qty / position_qty if position_qty > 0 else 1.0
            allocated_entry_fee = float(row["entry_fee"]) * ratio
            gross = (float(exit_price) - float(row["entry_price"])) * sold_qty
            pnl = gross - allocated_entry_fee - float(exit_fee)
            cost = float(row["entry_price"]) * sold_qty
            pnl_pct = pnl / cost if cost > 0 else 0.0
            now = datetime.now(timezone.utc).isoformat()

            if state_updates:
                for key, value in state_updates.items():
                    self._set_state_conn(conn, key, value)

            remaining = max(0.0, position_qty - sold_qty)
            if remaining > 1e-12:
                remaining_entry_fee = max(0.0, float(row["entry_fee"]) - allocated_entry_fee)
                conn.execute(
                    "UPDATE trades SET quantity=?, entry_fee=? WHERE id=?",
                    (remaining, remaining_entry_fee, trade_id),
                )
                cur = conn.execute(
                    """
                    INSERT INTO trades(
                        mode, symbol, quantity, entry_price, exit_price,
                        entry_fee, exit_fee, pnl, pnl_pct, status,
                        entry_reason, exit_reason, stop_price, take_profit_price,
                        opened_at, closed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["mode"], row["symbol"], sold_qty, row["entry_price"], exit_price,
                        allocated_entry_fee, exit_fee, pnl, pnl_pct,
                        row["entry_reason"], reason, row["stop_price"], row["take_profit_price"],
                        row["opened_at"], now,
                    ),
                )
                closed_id = int(cur.lastrowid)
                return {
                    "id": closed_id,
                    "source_trade_id": trade_id,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "partial": True,
                    "executed_quantity": sold_qty,
                    "remaining_quantity": remaining,
                }

            conn.execute(
                """
                UPDATE trades
                SET exit_price=?, exit_fee=?, pnl=?, pnl_pct=?, status='CLOSED',
                    exit_reason=?, closed_at=?
                WHERE id=?
                """,
                (exit_price, exit_fee, pnl, pnl_pct, reason, now, trade_id),
            )
            return {
                "id": trade_id,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "partial": False,
                "executed_quantity": sold_qty,
                "remaining_quantity": 0.0,
            }

    def get_open_trade(self, symbol: str, mode: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM trades
                WHERE symbol=? AND mode=? AND status='OPEN'
                ORDER BY id DESC LIMIT 1
                """,
                (symbol, mode),
            ).fetchone()
            return dict(row) if row else None

    def list_trades(
        self,
        limit: int = 100,
        *,
        mode: str | None = None,
        symbol: str | None = None,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if mode is not None:
            clauses.append("mode=?")
            params.append(mode)
        if symbol is not None:
            clauses.append("symbol=?")
            params.append(symbol)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM trades{where} ORDER BY id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
            return [dict(row) for row in rows]

    def closed_trades(
        self,
        limit: int | None = None,
        *,
        mode: str | None = None,
        symbol: str | None = None,
    ) -> list[dict]:
        clauses = ["status='CLOSED'"]
        params: list[Any] = []
        if mode is not None:
            clauses.append("mode=?")
            params.append(mode)
        if symbol is not None:
            clauses.append("symbol=?")
            params.append(symbol)
        sql = f"SELECT * FROM trades WHERE {' AND '.join(clauses)} ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def realized_pnl_today(self, *, mode: str, symbol: str) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(pnl),0) AS total
                FROM trades
                WHERE status='CLOSED'
                  AND mode=?
                  AND symbol=?
                  AND substr(closed_at,1,10)=?
                """,
                (mode, symbol, today),
            ).fetchone()
            return float(row["total"] or 0.0)

    def performance_summary(self, *, mode: str, symbol: str) -> dict:
        """Return realized analytics for all closed trades in this mode/symbol."""
        trades = list(reversed(self.closed_trades(mode=mode, symbol=symbol)))
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
            max_drawdown = max(max_drawdown, peak - cumulative)
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

    def record_order_intent(
        self,
        *,
        mode: str,
        symbol: str,
        client_order_id: str,
        side: str,
        requested_quantity: float,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO orders(
                    mode, symbol, client_order_id, side, requested_quantity,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING_SUBMIT', ?, ?)
                """,
                (mode, symbol, client_order_id, side, requested_quantity, now, now),
            )

    def update_order_record(
        self,
        *,
        client_order_id: str,
        binance_order_id: str | None,
        executed_quantity: float,
        average_fill_price: float | None,
        status: str,
        commission_quote: float,
        commission_details: list[dict] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE orders
                SET binance_order_id=?, executed_quantity=?, average_fill_price=?,
                    status=?, commission_quote=?, commission_details=?, updated_at=?
                WHERE client_order_id=?
                """,
                (
                    binance_order_id,
                    executed_quantity,
                    average_fill_price,
                    status,
                    commission_quote,
                    json.dumps(commission_details or []),
                    now,
                    client_order_id,
                ),
            )

    def get_order_by_client_id(self, client_order_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
            return dict(row) if row else None
