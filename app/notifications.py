"""Optional outbound notifications for critical trading events.

Deliberately tiny and failure-isolated: notifications are fire-and-forget,
never block or crash the trading engine, and are silenced per event key for
a configured interval so a repeating error cannot spam the channel. Every
destination is disabled while its setting is blank.

Supported destinations:
- A generic webhook (``NOTIFY_WEBHOOK_URL``) receiving JSON
  ``{event, text, mode, symbol, timestamp}`` via POST.
- A Telegram bot chat (``NOTIFY_TELEGRAM_BOT_TOKEN`` +
  ``NOTIFY_TELEGRAM_CHAT_ID``) via the Bot API sendMessage.

Secrets live only in configuration; they are never logged by this module.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 10.0


class Notifier:
    """Send event texts to the configured webhook/Telegram destinations."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._transport = transport
        self._last_sent_monotonic: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.notify_webhook_url
            or (
                self.settings.notify_telegram_bot_token
                and self.settings.notify_telegram_chat_id
            )
        )

    def _cooldown_active(self, event_key: str) -> bool:
        last = self._last_sent_monotonic.get(event_key)
        if last is None:
            return False
        return (time.monotonic() - last) < self.settings.notify_min_interval_seconds

    def _mark_sent(self, event_key: str) -> None:
        self._last_sent_monotonic[event_key] = time.monotonic()

    async def send(self, event_key: str, text: str) -> bool:
        """Deliver to all configured destinations. Returns True if anything sent."""
        if not self.enabled or self._cooldown_active(event_key):
            return False

        payload: dict[str, Any] = {
            "event": event_key,
            "text": text,
            "mode": self.settings.mode,
            "symbol": self.settings.symbol,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        delivered = False
        if self.settings.notify_webhook_url:
            if await self._post(self.settings.notify_webhook_url, payload):
                delivered = True
        if self.settings.notify_telegram_bot_token and self.settings.notify_telegram_chat_id:
            url = (
                "https://api.telegram.org/bot"
                f"{self.settings.notify_telegram_bot_token}/sendMessage"
            )
            if await self._post(
                url,
                {"chat_id": self.settings.notify_telegram_chat_id, "text": f"[{event_key}] {text}"},
            ):
                delivered = True

        if delivered:
            self._mark_sent(event_key)
        return delivered

    async def _post(self, url: str, payload: dict[str, Any]) -> bool:
        try:
            async with httpx.AsyncClient(
                timeout=HTTP_TIMEOUT_SECONDS,
                transport=self._transport,
            ) as client:
                response = await client.post(url, json=payload)
            if response.is_error:
                logger.warning(
                    "Notification to %s failed with HTTP %s",
                    url.split("//")[-1].split("/")[0],
                    response.status_code,
                )
                return False
            return True
        except Exception as exc:
            # A broken notification channel must never disturb trading.
            logger.warning("Notification delivery failed: %s", exc)
            return False

    def fire_and_forget(self, event_key: str, text: str) -> None:
        """Schedule delivery without blocking or raising in the caller."""
        if not self.enabled:
            return
        task = asyncio.create_task(self.send(event_key, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
