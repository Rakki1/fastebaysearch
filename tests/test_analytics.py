import asyncio
from datetime import datetime, timezone
from unittest.mock import Mock

import aiohttp
import pytest

from fastebaysearch_app.analytics import AnalyticsError, get_browse_quota, parse_browse_quota
from fastebaysearch_app.ebay_client import EbayClient
from test_search_reliability import Response, Session


RESET = "2026-11-15T08:00:00.000Z"


def payload(remaining=0, reset=RESET, bulk_remaining=5000, **overrides):
    rate = dict(count=5030 if remaining == 0 else 20, limit=5000, remaining=remaining,
                reset=reset, timeWindow=86400)
    rate.update(overrides)
    return {"rateLimits": [{"apiContext": "buy", "apiName": "Browse", "apiVersion": "v1",
                            "resources": [{"name": "buy.browse", "rates": [rate]},
                                          {"name": "buy.browse.item.bulk", "rates": [dict(rate, remaining=bulk_remaining)]}]}]}


def test_parse_actual_response_and_keep_bulk_quota_separate():
    quota = parse_browse_quota(payload())
    assert quota.exhausted
    assert len(quota.rates) == 1
    assert quota.rates[0].count == 5030  # Count may exceed the limit after rejected calls.
    assert quota.reset_at(0) == datetime(2026, 11, 15, 8, tzinfo=timezone.utc).timestamp()
    assert "remaining=0" in quota.summary()
    assert not parse_browse_quota(payload(remaining=4980, bulk_remaining=0)).exhausted


@pytest.mark.parametrize("data", [None, [], {}, {"rateLimits": []},
    {"rateLimits": [{"apiContext": "sell", "apiName": "Browse"}]},
    payload(remaining=True), payload(limit=0), payload(count=-1), payload(timeWindow="86400"),
    payload(remaining=-1), payload(remaining=5001)])
def test_unusable_quota_response_is_not_treated_as_available(data):
    with pytest.raises(AnalyticsError):
        parse_browse_quota(data)


@pytest.mark.parametrize("reset", [None, "invalid", "2026-11-15T08:00:00"])
def test_unknown_reset_does_not_invent_a_resume_time(reset):
    quota = parse_browse_quota(payload(reset=reset))
    assert quota.exhausted
    assert quota.reset_at(0) is None


def test_all_exhausted_windows_must_reset_before_retry():
    data = payload()
    rates = data["rateLimits"][0]["resources"][0]["rates"]
    rates.append(dict(rates[0], reset="2026-11-15T09:00:00Z", timeWindow=3600))
    quota = parse_browse_quota(data)
    assert quota.reset_at(0) == datetime(2026, 11, 15, 9, tzinfo=timezone.utc).timestamp()
    assert quota.reset_at(datetime(2026, 11, 15, 8, 30, tzinfo=timezone.utc).timestamp()) is None


def test_analytics_does_not_consume_browse_budget_or_need_marketplace(monkeypatch):
    session = Session([Response(data=payload())])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    client = EbayClient("token", max_search_calls=1)
    client._rate_limited.set()  # Lookup must still work after a Browse 429.
    quota = asyncio.run(get_browse_quota(client))
    assert quota.exhausted
    assert client.search_calls == 0
    assert session.calls[0]["params"] == {"api_context": "buy", "api_name": "browse"}
    assert "X-EBAY-C-MARKETPLACE-ID" not in session.calls[0]["headers"]


@pytest.mark.parametrize("status", [204, 400, 403, 429])
def test_analytics_errors_do_not_set_browse_rate_limit_flag(monkeypatch, status):
    session = Session([Response(status)])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    client = EbayClient("token")
    with pytest.raises(AnalyticsError, match=f"HTTP {status}"):
        asyncio.run(get_browse_quota(client))
    assert not client._rate_limited.is_set()
    assert len(session.calls) == 1


def test_analytics_auth_refresh_is_bounded(monkeypatch):
    auth = Mock()
    auth.get_access_token.return_value = "refreshed"
    session = Session([Response(401), Response(data=payload(remaining=10))])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    quota = asyncio.run(get_browse_quota(EbayClient("token", auth=auth)))
    assert not quota.exhausted
    auth.get_access_token.assert_called_once_with(force_refresh=True)
    assert session.calls[-1]["headers"]["Authorization"] == "Bearer refreshed"


def test_transient_analytics_failures_retry_without_exposing_response_data(monkeypatch):
    async def no_wait(_):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    session = Session([Response(503), asyncio.TimeoutError(), Response(data=payload())])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    assert asyncio.run(get_browse_quota(EbayClient("token"))).exhausted
    assert len(session.calls) == 3


@pytest.mark.parametrize("remaining,expected", [(0, 2), (100, 0)])
def test_quota_command_uses_analytics_without_browse_db_or_notifications(tmp_path, monkeypatch, capsys, remaining, expected):
    from fastebaysearch_app import cli
    from test_cli import minimal_config, write_config, FakeEbayAuth
    data = minimal_config()
    data["use_email"] = True  # Missing SMTP settings must not matter in the read-only quota mode.
    data["use_telegram"] = True
    path = write_config(tmp_path, data)
    monkeypatch.setattr(cli, "EbayAuth", FakeEbayAuth)

    async def lookup(self):
        return parse_browse_quota(payload(remaining=remaining))

    def forbidden(*args, **kwargs):
        pytest.fail("Quota command must not open the database, search listings or send notifications")

    monkeypatch.setattr(EbayClient, "get_browse_quota", lookup)
    monkeypatch.setattr(EbayClient, "run_queries", forbidden)
    monkeypatch.setattr(cli, "Database", forbidden)
    assert asyncio.run(cli.run_api_quota(path, tmp_path)) == expected
    output = capsys.readouterr().out
    assert "limit=5000" in output
    assert "has not been measured" in output
    assert "Recommended:" not in output
    assert not (tmp_path / "items.db").exists()


def test_quota_command_is_exclusive_with_clean_search():
    from fastebaysearch_app.cli import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(["config.json", "--clean-search", "--api-quota"])
