import asyncio
from unittest.mock import Mock

import aiohttp
import pytest

from fastebaysearch_app.ebay_client import EbayAuth, EbayClient, RateLimitError, parse_search_results
from fastebaysearch_app.models import SearchQuery


class Response:
    def __init__(self, status=200, data=None):
        self.status = status
        self.data = {"total": 0} if data is None else data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class Session:
    def __init__(self, responses):
        self.responses = responses if callable(responses) else iter(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        response = self.responses(kwargs) if callable(self.responses) else next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def search(session, client=None):
    return asyncio.run((client or EbayClient("token"))._search_with_session(session, "EBAY_GB", "camera"))


@pytest.fixture
def delays(monkeypatch):
    calls = []

    async def sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return calls


@pytest.mark.parametrize("options,expected", [
    (None, "FIXED_PRICE|AUCTION"), (["AUCTION"], "AUCTION"), (["FIXED_PRICE"], "FIXED_PRICE"),
])
def test_search_sends_buying_options_and_newest_sort(options, expected):
    session = Session([Response()])
    outcome = search(session, EbayClient("token", buying_options=options))
    assert outcome.status == "success"
    assert outcome.raw_items == []
    assert session.calls[0]["params"] == {
        "q": "camera", "limit": "200", "offset": "0", "sort": "newlyListed",
        "filter": f"buyingOptions:{{{expected}}}",
    }


@pytest.mark.parametrize("failure", [Response(500), Response(502), Response(503), Response(504),
                                     OSError("connection lost"), asyncio.TimeoutError(), aiohttp.ClientError()])
def test_transient_failures_retry_twice_on_same_page(failure, delays):
    session = Session([failure, failure, Response()])
    assert search(session).status == "success"
    assert delays == [1, 2]
    assert [c["params"]["offset"] for c in session.calls] == ["0", "0", "0"]


def test_exhausted_retries_preserve_previous_page(delays):
    first = [{"itemId": str(10000 + i)} for i in range(200)]
    session = Session([Response(data={"total": 400, "itemSummaries": first}),
                       Response(500), Response(500), Response(500)])
    outcome = search(session)
    assert outcome.status == "failed"
    assert outcome.raw_items == first
    assert outcome.pages == 1
    assert outcome.error == "HTTP 500"
    assert [c["params"]["offset"] for c in session.calls] == ["0", "200", "200", "200"]
    assert delays == [1, 2]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_errors_are_not_empty_successes(status, delays):
    session = Session([Response(status)])
    outcome = search(session)
    assert outcome.status == "failed"
    assert outcome.pages == 0
    assert len(session.calls) == 1
    assert delays == []


@pytest.mark.parametrize("data", [[], {"errors": [{"errorId": 123}]}, {}, {"total": "oops"},
                                 {"total": True}, {"total": -1}, {"total": 2, "itemSummaries": {}},
                                 {"total": 2, "itemSummaries": [None]}, ValueError("bad json")])
def test_bad_payload_is_failure(data, delays):
    session = Session([Response(data=data)])
    assert search(session).status == "failed"
    assert len(session.calls) == 1


def test_short_page_does_not_claim_full_coverage():
    outcome = search(Session([Response(data={"total": 250, "itemSummaries": [{"itemId": "12345"}]})]))
    assert outcome.status == "failed"
    assert len(outcome.raw_items) == 1


@pytest.mark.parametrize("total,status", [(9999, "success"), (10000, "success"), (10001, "truncated")])
def test_result_cap_and_sort_on_every_page(total, status):
    def respond(kwargs):
        params = kwargs["params"]
        offset, limit = int(params["offset"]), int(params["limit"])
        return Response(data={"total": total, "itemSummaries": [
            {"itemId": str(i + 10000)} for i in range(offset, min(offset + limit, total))]})

    session = Session(respond)
    outcome = search(session)
    assert outcome.status == status
    assert len(outcome.raw_items) == min(total, 10000)
    assert outcome.total == total
    assert len(session.calls) == 50
    assert all(c["params"]["sort"] == "newlyListed" for c in session.calls)
    assert int(session.calls[-1]["params"]["offset"]) == 9800


def test_concurrent_401s_share_one_refresh(monkeypatch):
    auth = Mock()
    auth.get_access_token.return_value = "new-token"
    client = EbayClient("old-token", concurrency_limit=4, auth=auth)
    session = Session(lambda kw: Response(401) if kw["headers"]["Authorization"] == "Bearer old-token"
                      else Response())
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    run = asyncio.run(client.run_queries(["EBAY_GB"], [SearchQuery(str(i), str(i)) for i in range(4)], {}))
    assert run.exit_code == 0
    assert len(run.outcomes) == 4
    auth.get_access_token.assert_called_once_with(force_refresh=True)
    assert all(o.status == "success" for o in run.outcomes)


@pytest.mark.parametrize("refresh_error", [False, True])
def test_failed_refresh_or_rejected_new_token_is_bounded(refresh_error, monkeypatch):
    auth = Mock()
    if refresh_error:
        auth.get_access_token.side_effect = RuntimeError("safe failure")
    else:
        auth.get_access_token.return_value = "new-token"
    session = Session(lambda kw: Response(401))
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    run = asyncio.run(EbayClient("old-token", auth=auth).run_queries(
        ["EBAY_GB"], [SearchQuery("a", "a"), SearchQuery("b", "b")], {}))
    assert run.exit_code == 1
    assert all(o.status == "failed" for o in run.outcomes)
    assert auth.get_access_token.call_count == 1
    assert len(session.calls) <= 4


def test_force_refresh_ignores_unexpired_cache(tmp_path, monkeypatch):
    import json
    import time
    import requests
    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps({"access_token": "old", "expires_at": time.time() + 9999}))
    response = Mock()
    response.json.return_value = {"access_token": "new", "expires_in": 7200}
    post = Mock(return_value=response)
    monkeypatch.setattr(requests, "post", post)
    auth = EbayAuth(token_file, "fake-id", "fake-secret")
    assert auth.get_access_token() == "old"
    assert post.call_count == 0
    assert auth.get_access_token(force_refresh=True) == "new"
    assert post.call_count == 1
    assert json.loads(token_file.read_text())["access_token"] == "new"


def test_real_429_path_marks_run_and_stops_queued_searches(monkeypatch):
    session = Session([Response(429)])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    run = asyncio.run(EbayClient("token", concurrency_limit=1).run_queries(
        ["EBAY_GB"], [SearchQuery("one", "one"), SearchQuery("two", "two")], {}))
    assert run.exit_code == 2
    assert [o.status for o in run.outcomes] == ["rate_limited", "cancelled"]
    assert len(session.calls) == 1


def test_rate_limit_keeps_partial_pages_from_cancelled_search():
    class Client(EbayClient):
        def __init__(self):
            super().__init__("token", concurrency_limit=2)
            self.second_page_started = asyncio.Event()

        async def _get_page(self, session, site, params):
            if params["q"] == "rate":
                await self.second_page_started.wait()
                self._rate_limited.set()
                raise RateLimitError()
            if params["offset"] == "0":
                return {"total": 400, "itemSummaries": [{"itemId": str(i + 10000)} for i in range(200)]}
            self.second_page_started.set()
            await asyncio.Event().wait()

    run = asyncio.run(Client().run_queries(["EBAY_GB"], [SearchQuery(q, q) for q in ["partial", "rate", "queued"]], {}))
    assert run.exit_code == 2
    assert len(run.items) == 200
    assert [o.status for o in run.outcomes] == ["cancelled", "rate_limited", "cancelled"]


def test_one_failed_search_does_not_discard_successful_search(monkeypatch):
    session = Session(lambda kw: Response(403) if kw["params"]["q"] == "bad" else
                      Response(data={"total": 1, "itemSummaries": [{"itemId": "12345"}]}))
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    run = asyncio.run(EbayClient("token").run_queries(["EBAY_GB"], [SearchQuery(q, q) for q in ["bad", "good"]], {}))
    assert run.exit_code == 1
    assert [item.item_id for item in run.items] == ["12345"]
    assert "failed=1" in run.summary and "success=1" in run.summary


def test_auction_bid_and_missing_prices():
    items = [{"itemId": "11111", "currentBidPrice": {"value": "12", "currency": "USD"}},
             {"itemId": "22222", "price": {"value": "nan", "currency": "EUR"}},
             {"itemId": "33333"},
             {"itemId": "44444", "price": {"value": "0", "currency": "EUR"}}]
    assert [item.price for item in parse_search_results("camera", items, {"USD": 1.2})] == [
        "Current bid: 10.00 EUR (12.00 USD)", "Price unavailable", "Price unavailable", "0.00 EUR"]


def test_result_cap_with_page_size_not_dividing_10000():
    def respond(kwargs):
        offset = int(kwargs["params"]["offset"])
        return Response(data={"total": 10001, "itemSummaries": [
            {"itemId": str(i + 10000)} for i in range(offset, min(offset + 128, 10001))]})

    outcome = asyncio.run(EbayClient("token")._search_with_session(Session(respond), "EBAY_GB", "camera", limit=128))
    assert outcome.status == "truncated"
    assert len(outcome.raw_items) == 10000


def test_caller_cancellation_is_not_swallowed():
    async def run():
        started = asyncio.Event()
        ended = asyncio.Event()

        class WaitingClient(EbayClient):
            async def _get_page(self, *args):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    ended.set()

        task = asyncio.create_task(WaitingClient("token").run_queries(["EBAY_GB"], [SearchQuery("x", "x")], {}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ended.is_set()

    asyncio.run(run())
