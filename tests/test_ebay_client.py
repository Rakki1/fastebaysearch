import asyncio

from fastebaysearch_app.ebay_urls import build_item_web_url
import pytest

from fastebaysearch_app.ebay_client import EbayClient, RateLimitError, parse_search_results
from fastebaysearch_app.models import SearchQuery


def test_parse_search_results_handles_nullable_fields():
    raw_items = [
        {
            "itemId": "v1|1234567890|0",
            "price": None,
            "title": None,
            "listingMarketplaceId": None,
            "itemWebUrl": None,
            "seller": None,
            "image": {"imageUrl": "javascript:alert(1)"},
        },
        None,
    ]

    results = parse_search_results("camera", raw_items, {})

    assert len(results) == 1
    assert results[0].item_id == "1234567890"
    assert results[0].name == "Unknown"
    assert results[0].price == "0.00 EUR"
    assert results[0].link == ""
    assert results[0].image == ""


def test_parse_search_results_converts_known_currency():
    raw_items = [
        {
            "legacyItemId": "55555",
            "price": {"value": "12", "currency": "USD"},
            "title": "Lens",
            "listingMarketplaceId": "EBAY_US",
            "itemWebUrl": "https://example.com/item?hash=1",
            "seller": {"username": "seller"},
        }
    ]

    results = parse_search_results("lens", raw_items, {"USD": 1.2})

    assert results[0].price == "10.00 EUR (12.00 USD)"
    assert results[0].link == "https://www.ebay.com/itm/55555"


def test_parse_search_results_builds_link_from_listing_marketplace():
    raw_items = [
        {
            "itemId": "v1|1234567890|0",
            "title": "US listing surfaced on Italy",
            "listingMarketplaceId": "EBAY_US",
            "itemWebUrl": "https://www.ebay.it/itm/1234567890?hash=abc",
        },
        {
            "legacyItemId": "9876543210",
            "title": "Italian listing",
            "listingMarketplaceId": "EBAY_IT",
            "itemWebUrl": "https://www.ebay.com/itm/9876543210?hash=def",
        },
    ]

    results = parse_search_results("lens", raw_items, {})

    assert results[0].ebay_site == "EBAY_US"
    assert results[0].link == "https://www.ebay.com/itm/1234567890"
    assert results[1].ebay_site == "EBAY_IT"
    assert results[1].link == "https://www.ebay.it/itm/9876543210"


def test_build_item_web_url_falls_back_for_unknown_marketplace():
    link = build_item_web_url(
        "1234567890",
        "EBAY_UNKNOWN",
        "https://example.com/item/1234567890?hash=abc",
    )

    assert link == "https://example.com/item/1234567890"


def test_build_item_web_url_rejects_unsafe_fallback():
    assert build_item_web_url("1234567890", "EBAY_UNKNOWN", "javascript:alert(1)") == ""


class FakeSearchResponse:
    status = 200
    headers = {}

    def __init__(self, offset, limit, total):
        self.offset = offset
        self.limit = limit
        self.total = total

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return {
            "total": self.total,
            "itemSummaries": [
                {"itemId": f"v1|{self.offset + index}|0"}
                for index in range(self.limit)
            ],
        }


class FakeSearchSession:
    def __init__(self, total):
        self.total = total
        self.offsets = []

    def get(self, url, headers, params, timeout):
        offset = int(params["offset"])
        limit = int(params["limit"])
        self.offsets.append(offset)
        return FakeSearchResponse(offset, limit, self.total)


def test_search_paginates_past_offset_1000():
    session = FakeSearchSession(total=1200)
    client = EbayClient("token")

    results = asyncio.run(client._search_with_session(session, "EBAY_US", "camera", limit=200))

    assert session.offsets == [0, 200, 400, 600, 800, 1000]
    assert len(results) == 1200


class OutOfOrderClient(EbayClient):
    async def _search_with_session(self, session, site, full_query, limit=200):
        if full_query == "slow":
            await asyncio.sleep(0.01)
        item_id = "10001" if full_query == "slow" else "10002"
        return [{"itemId": item_id, "title": full_query, "listingMarketplaceId": site}]


def test_run_queries_preserves_configured_order_when_tasks_complete_out_of_order():
    client = OutOfOrderClient("token")
    queries = [
        SearchQuery("slow", "slow"),
        SearchQuery("fast", "fast"),
    ]

    results = asyncio.run(client.run_queries(["EBAY_US"], queries, {}))

    assert [result.item_id for result in results] == ["10001", "10002"]


class CancellingClient(EbayClient):
    def __init__(self):
        super().__init__("token", concurrency_limit=2)
        self.cancelled = False

    async def _search_with_session(self, session, site, full_query, limit=200):
        if full_query == "rate-limit":
            raise RateLimitError("429 rate limit")
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return [{"itemId": "10003", "title": full_query, "listingMarketplaceId": site}]


def test_run_queries_cancels_remaining_tasks_when_one_task_raises():
    client = CancellingClient()
    queries = [
        SearchQuery("slow", "slow"),
        SearchQuery("rate-limit", "rate-limit"),
    ]

    with pytest.raises(RateLimitError):
        asyncio.run(client.run_queries(["EBAY_US"], queries, {}))

    assert client.cancelled is True
