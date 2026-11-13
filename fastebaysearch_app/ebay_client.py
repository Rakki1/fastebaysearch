from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import time
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import replace
from urllib.parse import urlparse, parse_qs
from decimal import Decimal, InvalidOperation
from typing import Any

from .ebay_urls import build_item_web_url
from .models import QueryOutcome, SearchQuery, SearchResult, SearchRun
from .utils import clean_ebay_id, format_date, safe_url, scalar_text, write_private_json_atomic

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EXCHANGE_RATE_URL = "https://api.frankfurter.app/latest?base=EUR&symbols=USD,GBP,AUD"
EBAY_MAX_OFFSET = 9999
SEARCH_RETRIES = 2
TRANSIENT_STATUSES = {500, 502, 503, 504}


class BudgetError(Exception):
    pass


def amount(data):
    try:
        value = Decimal(str(data.get("value")))
        return str(value) if value.is_finite() and value >= 0 else ""
    except (AttributeError, InvalidOperation):
        return ""


class RateLimitError(Exception):
    pass


class SearchRequestError(Exception):
    """A safe, credential-free description of an unsuccessful search page."""


def parse_search_results(base_name: str, raw_items: list[Any], rates: dict[str, float], logger=None) -> list[SearchResult]:
    results: list[SearchResult] = []
    for item in raw_items:
        if not isinstance(item, dict):
            if logger:
                logger.debug(f"Skipping non-object search item: {item!r}")
            continue

        item_id = clean_ebay_id(item.get("itemId") or item.get("legacyItemId"))
        if not item_id:
            continue

        is_bid = not item.get("price") and bool(item.get("currentBidPrice"))
        price_data = item.get("price") or item.get("currentBidPrice") or {}
        if not isinstance(price_data, dict):
            price_data = {}
        currency = scalar_text(price_data.get("currency")).upper()
        try:
            value = float(price_data.get("value"))
        except (TypeError, ValueError):
            value = float("nan")

        if not currency or not math.isfinite(value) or value < 0:
            display_price = "Price unavailable"
        elif currency == "EUR":
            display_price = f"{value:.2f} EUR"
        elif (isinstance(rates.get(currency), (int, float))
              and math.isfinite(rates[currency]) and rates[currency] > 0):
            display_price = f"{value / rates[currency]:.2f} EUR ({value:.2f} {currency})"
        else:
            display_price = f"{value:.2f} {currency}"
        if is_bid and display_price != "Price unavailable":
            display_price = f"Current bid: {display_price}"

        image = item.get("image") or {}
        image_url = safe_url(image.get("imageUrl", "")) if isinstance(image, dict) else ""
        seller = item.get("seller") or {}
        if not isinstance(seller, dict):
            seller = {}

        listing_marketplace_id = scalar_text(item.get("listingMarketplaceId"), "Unknown")
        results.append(
            SearchResult(
                keywords=str(base_name),
                name=scalar_text(item.get("title"), "Unknown"),
                ebay_site=listing_marketplace_id,
                price=display_price,
                item_id=item_id,
                link=build_item_web_url(item_id, listing_marketplace_id, item.get("itemWebUrl")),
                seller=scalar_text(seller.get("username"), "Unknown"),
                starts=format_date(item.get("itemCreationDate"), logger),
                ends=format_date(item.get("itemEndDate"), logger),
                image=image_url,
                buying_options=tuple(sorted(item.get("buyingOptions") or [])),
                fixed_amount=amount(item.get("price")),
                fixed_currency=(item.get("price") if isinstance(item.get("price"), dict) else {}).get("currency", ""),
                bid_amount=amount(item.get("currentBidPrice")),
                bid_currency=(item.get("currentBidPrice") if isinstance(item.get("currentBidPrice"), dict) else {}).get("currency", ""),
            )
        )
    return results


class EbayAuth:
    def __init__(
        self,
        token_file: Path,
        client_id: str,
        client_secret: str,
        logger: logging.Logger | None = None,
    ):
        self.token_file = Path(token_file)
        self.client_id = client_id
        self.client_secret = client_secret
        self.logger = logger or logging.getLogger("fastebaysearch")

    def get_access_token(self, force_refresh: bool = False) -> str:
        try:
            if not force_refresh and self.token_file.exists():
                with self.token_file.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if (isinstance(data, dict) and data.get("access_token")
                        and time.time() < float(data.get("expires_at", 0))):
                    return str(data["access_token"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            self.logger.debug("Token cache read failed: %s", type(exc).__name__)

        import requests

        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        try:
            response = requests.post(
                TOKEN_URL,
                headers={
                    "Authorization": f"Basic {auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "client_credentials",
                    "scope": "https://api.ebay.com/oauth/api_scope",
                },
                timeout=20,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"Token retrieval failed: {type(exc).__name__}") from None

        data = response.json()
        if not isinstance(data, dict) or not data.get("access_token"):
            raise RuntimeError("Token retrieval returned an invalid response")
        try:
            expires_in = int(data["expires_in"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Token retrieval response is missing a valid expires_in value") from exc

        data["expires_at"] = time.time() + expires_in - 60
        write_private_json_atomic(self.token_file, data)
        return str(data["access_token"])


class EbayClient:
    def __init__(self, token: str, logger: logging.Logger | None = None, concurrency_limit: int = 8,
                 buying_options: list[str] | None = None, auth: EbayAuth | None = None, search_in_description: bool = True,
                 max_search_calls: int = 500, exclude_category_ids: dict[str, list[str]] | None = None):
        self.exclude_category_ids = exclude_category_ids or {}
        self.search_in_description = search_in_description
        self.max_search_calls = max_search_calls
        self._page_semaphore = asyncio.Semaphore(max(1, int(concurrency_limit)))
        self.search_calls = 0
        self.run_end = int(datetime.now(timezone.utc).timestamp() * 1000)
        self.token = token
        self.logger = logger or logging.getLogger("fastebaysearch")
        self.concurrency_limit = max(1, int(concurrency_limit))
        self.buying_options = buying_options if buying_options is not None else ["FIXED_PRICE", "AUCTION"]
        self.auth = auth
        self._refresh_lock = asyncio.Lock()
        self._refresh_attempted = False
        self._token_generation = 0
        self._rate_limited = asyncio.Event()

    async def get_exchange_rates(self) -> dict[str, float]:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(EXCHANGE_RATE_URL, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        rates = data.get("rates", {}) if isinstance(data, dict) else {}
                        self.logger.info(f"Exchange rates updated: {rates}")
                        return dict(rates)
                    self.logger.warning(f"Frankfurter API error {response.status}. Using original currencies.")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
            self.logger.error(f"Exchange rate fetch failed: {exc}")
        return {}

    async def _refresh_token(self, failed_generation: int) -> bool:
        async with self._refresh_lock:
            if self._token_generation != failed_generation:
                return True
            if self._refresh_attempted or self.auth is None:
                return False
            self._refresh_attempted = True
            try:
                self.token = await asyncio.to_thread(self.auth.get_access_token, force_refresh=True)
            except (OSError, RuntimeError, ValueError) as exc:
                self.logger.error("Token refresh failed: %s", type(exc).__name__)
                return False
            self._token_generation += 1
            return True

    async def _get_page(self, session, site: str, params: dict[str, str]) -> dict[str, Any]:
        async with self._page_semaphore:
            return await self._request_page(session, site, params)

    async def _request_page(self, session, site: str, params: dict[str, str]) -> dict[str, Any]:
        import aiohttp

        retries = 0
        auth_retried = False
        while True:
            if self._rate_limited.is_set():
                raise asyncio.CancelledError
            if self.search_calls >= self.max_search_calls:
                raise BudgetError("Browse call budget exhausted")
            self.search_calls += 1
            generation = self._token_generation
            headers = {"Authorization": f"Bearer {self.token}",
                       "X-EBAY-C-MARKETPLACE-ID": site, "Accept": "application/json"}
            status = None
            try:
                async with session.get(SEARCH_URL, headers=headers, params=params,
                                       timeout=aiohttp.ClientTimeout(total=30)) as response:
                    status = response.status
                    if response.status == 429:
                        self._rate_limited.set()
                        raise RateLimitError(f"429 rate limit ({site})")
                    if status == 200:
                        try:
                            data = await response.json()
                        except (ValueError, aiohttp.ContentTypeError):
                            raise SearchRequestError("Invalid JSON response") from None
                        if not isinstance(data, dict) or data.get("errors"):
                            raise SearchRequestError("Invalid response object or API error payload")
                        return data
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                reason = f"Network error: {type(exc).__name__}"
            else:
                if status == 401:
                    if not auth_retried and await self._refresh_token(generation):
                        auth_retried = True
                        continue
                    raise SearchRequestError("HTTP 401: authentication failed")
                if status not in TRANSIENT_STATUSES:
                    raise SearchRequestError(f"HTTP {status}")
                reason = f"HTTP {status}"
            if retries >= SEARCH_RETRIES:
                raise SearchRequestError(reason)
            retries += 1
            self.logger.warning("Retrying %s query=%r offset=%s (%s), retry=%s",
                                site, params["q"], params["offset"], reason, retries)
            await asyncio.sleep(retries)

    async def _search_with_session(self, session, site: str, full_query: str, limit: int = 200,
                                   outcome: QueryOutcome | None = None) -> QueryOutcome:
        if not 1 <= limit <= 200:
            raise ValueError("Search page limit must be between 1 and 200")
        if outcome is None:
            outcome = QueryOutcome(site, SearchQuery(full_query, full_query))
        def stamp(ms):
            return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

        intervals = [(788918400000, self.run_end)]
        incomplete = False
        seen = set()
        try:
            while intervals:
                low, high = intervals.pop()
                offset = 0
                while True:
                    filters = ["buyingOptions:{" + "|".join(self.buying_options) + "}",
                               f"itemStartDate:[{stamp(low)}..{stamp(high)}]"]
                    if self.search_in_description:
                        filters.append("searchInDescription:true")
                    if self.exclude_category_ids.get(site):
                        filters.append("excludeCategoryIds:{" + "|".join(self.exclude_category_ids[site]) + "}")
                    params = {"q": full_query, "limit": str(limit), "offset": str(offset),
                              "sort": "newlyListed", "filter": ",".join(filters)}
                    data = await self._get_page(session, site, params)
                    if "total" not in data and "itemSummaries" not in data:
                        raise SearchRequestError("Missing search results")
                    items = data.get("itemSummaries", [])
                    total = data.get("total")
                    if not isinstance(items, list) or len(items) > limit or any(not isinstance(i, dict) for i in items):
                        raise SearchRequestError("Invalid itemSummaries")
                    if total is not None and (type(total) is not int or total < 0):
                        raise SearchRequestError("Invalid total")
                    outcome.total = total
                    outcome.pages += 1
                    for item in items:
                        key = item.get("itemId") or item.get("legacyItemId")
                        if key not in seen:
                            seen.add(key)
                            outcome.raw_items.append(item)
                    split = (total is not None and total > 10000) or offset + len(items) >= 10000
                    if split:
                        if high - low <= 1:
                            incomplete = True
                        else:
                            mid = (low + high) // 2
                            intervals.extend([(low, mid), (mid, high)])
                        break
                    link = data.get("next")
                    if not link:
                        break
                    parsed = urlparse(link)
                    query = parse_qs(parsed.query)
                    try:
                        next_offset = int(query["offset"][0])
                    except (ValueError, KeyError, IndexError):
                        raise SearchRequestError("Invalid next offset") from None
                    if (parsed.scheme != "https" or parsed.netloc != "api.ebay.com"
                            or parsed.path != "/buy/browse/v1/item_summary/search"
                            or next_offset <= offset or next_offset % limit or next_offset > 9999):
                        raise SearchRequestError("Invalid next link")
                    offset = next_offset
                    await asyncio.sleep(0)
                await asyncio.sleep(0)
            outcome.status = "truncated" if incomplete else "success"
            if incomplete:
                outcome.error = "Unsplittable time interval reached the result cap"
        except BudgetError as exc:
            outcome.status = "truncated"
            outcome.error = str(exc)
        except RateLimitError:
            outcome.status = "rate_limited"
            outcome.error = "HTTP 429"
            raise
        except SearchRequestError as exc:
            outcome.status = "failed"
            outcome.error = str(exc)
        except asyncio.CancelledError:
            outcome.status = "cancelled"
            outcome.error = "Search interrupted"
            raise
        return outcome

    async def run_queries(self, sites: list[str], queries: list[SearchQuery], rates: dict[str, float]) -> SearchRun:
        import aiohttp

        self.search_calls = 0
        self.run_end = int(datetime.now(timezone.utc).timestamp() * 1000)
        self._rate_limited = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._refresh_attempted = False
        self._page_semaphore = asyncio.Semaphore(self.concurrency_limit)
        outcomes = [QueryOutcome(site, query) for site in sites for query in queries]
        for site in sites:
            self.logger.info("Excluded categories for %s: %s", site, self.exclude_category_ids.get(site, []))

        async def guarded_search(session, outcome):
            if not self._rate_limited.is_set():
                try:
                    await self._search_with_session(session, outcome.site, outcome.query.keywords, outcome=outcome)
                except asyncio.CancelledError:
                    # A rate-limit cancellation is a recorded outcome, not cancellation of the caller.
                    if not self._rate_limited.is_set():
                        raise

        async with aiohttp.ClientSession() as session:
            tasks = [asyncio.create_task(guarded_search(session, outcome)) for outcome in outcomes]
            self.logger.info("Launching %s searches with concurrency=%s", len(tasks), self.concurrency_limit)
            try:
                await asyncio.gather(*tasks)
            except RateLimitError:
                pass
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        run = SearchRun(outcomes=outcomes)
        for outcome in outcomes:
            run.items.extend(replace(item, source_site=outcome.site) for item in
                             parse_search_results(outcome.query.base_name, outcome.raw_items, rates, self.logger))
            log = self.logger.info if outcome.status == "success" else self.logger.warning
            log("Search %s: %s query=%r pages=%s reported=%s fetched=%s reason=%s",
                outcome.status, outcome.site, outcome.query.keywords, outcome.pages,
                outcome.total, len(outcome.raw_items), outcome.error)
        return run
