"""Process-wide logging configuration for the trading bot.

Kept deliberately small: one console handler, a stable single-line format,
and no third-party dependencies. The bot previously had almost no logging,
which made Testnet incidents impossible to reconstruct after the fact.

Secrets (API keys, signatures) must never be passed to these loggers.
"""

import logging
import os

_CONFIGURED = False

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(level: str | None = None) -> None:
    """Initialize root logging once. Safe to call multiple times."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    chosen = (level or os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    if chosen not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        chosen = "INFO"

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))

    root = logging.getLogger()
    root.setLevel(chosen)
    root.addHandler(handler)

    # Uvicorn configures its own loggers; align their verbosity with LOG_LEVEL
    # without duplicating handlers.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.setLevel(chosen)
        logger.propagate = False

    _CONFIGURED = True
