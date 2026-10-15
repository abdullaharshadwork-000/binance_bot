"""Tests for the optional outbound notification module."""

import asyncio
import json

import httpx

from app.config import Settings
from app.notifications import Notifier


def make_notifier(**overrides) -> tuple[Notifier, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    settings = Settings(
        _env_file=None,
        notify_webhook_url="https://hooks.example/test",
        notify_min_interval_seconds=overrides.pop("notify_min_interval_seconds", 300.0),
        **overrides,
    )
    return Notifier(settings, transport=httpx.MockTransport(handler)), requests


def test_disabled_without_destinations():
    notifier = Notifier(Settings(_env_file=None))
    assert notifier.enabled is False
    assert asyncio.run(notifier.send("trades", "hello")) is False


def test_webhook_receives_structured_payload():
    notifier, requests = make_notifier()
    delivered = asyncio.run(notifier.send("trades", "BTCUSDT BUY executed"))
    assert delivered is True
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["event"] == "trades"
    assert body["text"] == "BTCUSDT BUY executed"
    assert body["mode"] == "paper"
    assert body["symbol"] == "BTCUSDT"
    assert "timestamp" in body


def test_cooldown_suppresses_same_key_but_not_other_keys():
    notifier, requests = make_notifier(notify_min_interval_seconds=300.0)
    assert asyncio.run(notifier.send("errors", "first")) is True
    assert asyncio.run(notifier.send("errors", "second")) is False
    assert asyncio.run(notifier.send("trades", "different key")) is True
    assert len(requests) == 2


def test_http_error_is_isolated_and_returns_false():
    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    settings = Settings(_env_file=None, notify_webhook_url="https://hooks.example/test")
    notifier = Notifier(settings, transport=httpx.MockTransport(failing_handler))
    assert notifier.enabled is True
    assert asyncio.run(notifier.send("errors", "payload")) is False


def test_transport_exception_never_raises():
    def broken_handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("network gone")

    settings = Settings(_env_file=None, notify_webhook_url="https://hooks.example/test")
    notifier = Notifier(settings, transport=httpx.MockTransport(broken_handler))
    assert asyncio.run(notifier.send("errors", "payload")) is False


def test_telegram_destination_receives_message():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    settings = Settings(
        _env_file=None,
        notify_telegram_bot_token="123:abc",
        notify_telegram_chat_id="42",
    )
    notifier = Notifier(settings, transport=httpx.MockTransport(handler))
    assert notifier.enabled is True
    assert asyncio.run(notifier.send("lifecycle", "engine started")) is True
    assert len(requests) == 1
    assert "/bot123:abc/sendMessage" in str(requests[0].url)
    body = json.loads(requests[0].content)
    assert body["chat_id"] == "42"
    assert "[lifecycle] engine started" in body["text"]


def test_fire_and_forget_swallows_exceptions_without_blocking_event_loop():
    def broken_handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("network gone")

    settings = Settings(_env_file=None, notify_webhook_url="https://hooks.example/test")
    notifier = Notifier(settings, transport=httpx.MockTransport(broken_handler))

    async def scenario():
        notifier.fire_and_forget("errors", "payload")
        await asyncio.sleep(0.05)
        assert notifier._tasks == set()

    asyncio.run(scenario())
