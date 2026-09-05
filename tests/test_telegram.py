import asyncio

import aiohttp
import pytest

from fastebaysearch_app.config import TelegramConfig
from fastebaysearch_app.models import SearchResult
from fastebaysearch_app.notifications.telegram import TelegramNotifier

SECRET_URL = "https://api.telegram.org/botTEST-SECRET/sendMessage"
ITEM = SearchResult("camera", "Camera", "EBAY_GB", "10 GBP", "12345",
                    "https://www.ebay.co.uk/itm/12345", "seller", "", "", "https://example.com/image.jpg")


class Response:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, **kwargs):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.parametrize("response", [OSError(SECRET_URL), aiohttp.ClientError(SECRET_URL),
    asyncio.TimeoutError(SECRET_URL), Response(ValueError(SECRET_URL)), Response([]), Response(None),
    Response({"ok": False, "description": SECRET_URL}, 400)])
def test_telegram_failure_is_false_and_logs_no_token(response, caplog):
    notifier = TelegramNotifier(TelegramConfig("TEST-SECRET", "chat"))
    session = Session([response])
    assert asyncio.run(notifier._post(session, "https://example.invalid", SECRET_URL, {}, ITEM)) is False
    assert "TEST-SECRET" not in caplog.text


@pytest.mark.parametrize("response", [OSError(SECRET_URL), Response([]), Response(ValueError(SECRET_URL))])
def test_header_failure_is_safe(response, monkeypatch, caplog):
    session = Session([response])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: session)
    notifier = TelegramNotifier(TelegramConfig("TEST-SECRET", "chat"))
    assert asyncio.run(notifier.send_header(1, "PARTIAL SEARCH")) is False
    assert "TEST-SECRET" not in caplog.text


@pytest.mark.parametrize("fallback,expected", [(Response({"ok": True}), True),
    (Response({"ok": False}, 400), False), (Response([]), False), (OSError(SECRET_URL), False)])
def test_photo_text_fallback(fallback, expected, caplog):
    session = Session([Response({"ok": False}, 400), fallback])
    notifier = TelegramNotifier(TelegramConfig("TEST-SECRET", "chat", send_mode="auto"))
    actual = asyncio.run(notifier._post(session, "https://example.invalid", "https://example.invalid/sendPhoto",
                                       {"caption": "Camera"}, ITEM))
    assert actual is expected
    assert len(session.calls) == 2
    assert session.calls[-1][0].endswith("/sendMessage")
    assert "TEST-SECRET" not in caplog.text


def test_photo_only_does_not_fall_back():
    session = Session([Response({"ok": False}, 400)])
    notifier = TelegramNotifier(TelegramConfig("token", "chat", send_mode="photo_only"))
    assert asyncio.run(notifier._post(session, "https://example.invalid", "https://example.invalid/sendPhoto",
                                      {"caption": "Camera"}, ITEM)) is False
    assert len(session.calls) == 1


def test_partial_run_in_header(monkeypatch):
    session = Session([Response({"ok": True})])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: session)
    notifier = TelegramNotifier(TelegramConfig("token", "chat"))
    assert asyncio.run(notifier.send_header(3, "PARTIAL SEARCH: failed=1")) is True
    assert "PARTIAL SEARCH: failed=1" in session.calls[0][1]["json"]["text"]
