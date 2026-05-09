from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

from .models import SearchQuery, SearchResult
from .utils import clean_ebay_id, format_date, safe_url, scalar_text, write_private_json_atomic

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EXCHANGE_RATE_URL = "https://api.frankfurter.app/latest?base=EUR&symbols=USD,GBP,AUD"
EBAY_MAX_OFFSET = 9999


class RateLimitError(Exception):
    pass


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

        price_data = item.get("price") or {}
        if not isinstance(price_data, dict):
            price_data = {}
        currency = scalar_text(price_data.get("currency"), "EUR").upper() or "EUR"
        try:
            value = float(price_data.get("value", "0.0"))
        except (TypeError, ValueError):
            value = 0.0

        if currency == "EUR":
            display_price = f"{value:.2f} EUR"
        elif currency in rates:
            display_price = f"{value / rates[currency]:.2f} EUR ({value:.2f} {currency})"
        else:
            display_price = f"{value:.2f} {currency}"

        image = item.get("image") or {}
        image_url = safe_url(image.get("imageUrl", "")) if isinstance(image, dict) else ""
        seller = item.get("seller") or {}
        if not isinstance(seller, dict):
            seller = {}

        results.append(
            SearchResult(
                keywords=str(base_name),
                name=scalar_text(item.get("title"), "Unknown"),
                ebay_site=scalar_text(item.get("listingMarketplaceId"), "Unknown"),
                price=display_price,
                item_id=item_id,
                link=scalar_text(item.get("itemWebUrl")).split("?")[0],
                seller=scalar_text(seller.get("username"), "Unknown"),
                starts=format_date(item.get("itemCreationDate"), logger),
                ends=format_date(item.get("itemEndDate"), logger),
                image=image_url,
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

    def get_access_token(self) -> str:
        try:
            if self.token_file.exists():
                with self.token_file.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if time.time() < float(data.get("expires_at", 0)):
                    return str(data["access_token"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            self.logger.debug(f"Token cache read failed: {exc}")

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
            raise RuntimeError(f"Token retrieval failed: {exc}") from exc

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
    def __init__(self, token: str, logger: logging.Logger | None = None, concurrency_limit: int = 8):
        self.token = token
        self.logger = logger or logging.getLogger("fastebaysearch")
        self.concurrency_limit = max(1, int(concurrency_limit))

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

    async def _search_with_session(self, session, site: str, full_query: str, limit: int = 200) -> list[dict[str, Any]]:
        import aiohttp

        all_results: list[dict[str, Any]] = []
        offset = 0
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-EBAY-C-MARKETPLACE-ID": site,
            "Accept": "application/json",
        }
        timeout_settings = aiohttp.ClientTimeout(total=30)

        try:
            while True:
                params = {"q": full_query, "limit": str(limit), "offset": str(offset)}
                async with session.get(SEARCH_URL, headers=headers, params=params, timeout=timeout_settings) as response:
                    if response.status == 429:
                        retry_after = response.headers.get("Retry-After")
                        self.logger.warning(f"429 rate limit ({site}), Retry-After={retry_after}")
                        raise RateLimitError(f"429 rate limit ({site})")
                    if response.status != 200:
                        self.logger.error(f"API Error ({site}): {response.status} - {await response.text()}")
                        break

                    data = await response.json()
                    if not isinstance(data, dict):
                        self.logger.error(f"Unexpected API response shape ({site}): {type(data).__name__}")
                        break
                    items = data.get("itemSummaries") or []
                    if not isinstance(items, list):
                        self.logger.error(f"Unexpected itemSummaries shape ({site}): {type(items).__name__}")
                        break
                    all_results.extend(items)

                    try:
                        total = int(data.get("total") or 0)
                    except (TypeError, ValueError):
                        total = 0
                    next_offset = offset + limit
                    if total <= offset + len(items) or len(items) < limit or next_offset > EBAY_MAX_OFFSET:
                        break
                    offset = next_offset
        except RateLimitError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
            self.logger.error(f"API request failed ({site}): {exc}")
        return all_results

    async def run_queries(self, sites: list[str], queries: list[SearchQuery], rates: dict[str, float]) -> list[SearchResult]:
        import aiohttp

        semaphore = asyncio.Semaphore(self.concurrency_limit)

        async def guarded_search(session, site: str, query: SearchQuery) -> tuple[SearchQuery, list[dict[str, Any]]]:
            async with semaphore:
                return query, await self._search_with_session(session, site, query.keywords)

        async with aiohttp.ClientSession() as session:
            tasks = [guarded_search(session, site, query) for site in sites for query in queries]
            self.logger.info(f"Launching {len(tasks)} API requests with concurrency={self.concurrency_limit}...")
            raw_results = await asyncio.gather(*tasks)

        parsed: list[SearchResult] = []
        for query, items in raw_results:
            parsed.extend(parse_search_results(query.base_name, items, rates, self.logger))
        return parsed
