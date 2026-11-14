"""Developer Analytics calls use their own quota, separate from Browse requests."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

ANALYTICS_URL = "https://api.ebay.com/developer/analytics/v1_beta/rate_limit"


class AnalyticsError(RuntimeError):
    pass


@dataclass(frozen=True)
class QuotaRate:
    limit: int
    count: int | None
    remaining: int
    reset_at: float | None
    time_window: int


@dataclass(frozen=True)
class BrowseQuota:
    rates: tuple[QuotaRate, ...]

    @property
    def exhausted(self):
        return any(rate.remaining <= 0 for rate in self.rates)

    def reset_at(self, now: float) -> float | None:
        exhausted = [rate for rate in self.rates if rate.remaining <= 0]
        if not exhausted or any(rate.reset_at is None or rate.reset_at <= now for rate in exhausted):
            return None
        return max(rate.reset_at for rate in exhausted)

    def summary(self):
        return "; ".join(f"Browse: limit={r.limit}, used={r.count if r.count is not None else 'unknown'}, "
                         f"remaining={r.remaining}, window={r.time_window}s, reset={format_reset(r.reset_at)}"
                         for r in self.rates)


def format_reset(timestamp: float | None) -> str:
    if timestamp is None:
        return "unknown"
    utc = datetime.fromtimestamp(timestamp, timezone.utc)
    text = utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    local = utc.astimezone()
    if local.utcoffset() != utc.utcoffset():
        text += local.strftime(" (server time %Y-%m-%d %H:%M:%S %z)")
    return text


def parse_browse_quota(data: object) -> BrowseQuota:
    rates = []
    if not isinstance(data, dict) or not isinstance(data.get("rateLimits"), list):
        raise AnalyticsError("Invalid Analytics response")
    for api in data["rateLimits"]:
        if not isinstance(api, dict) or str(api.get("apiContext", "")).lower() != "buy" or str(api.get("apiName", "")).lower() != "browse":
            continue
        resources = api.get("resources")
        if not isinstance(resources, list):
            raise AnalyticsError("Missing Browse quota resources")
        for resource in resources:
            # getItems (bulk) has a separate quota and is not used by this application.
            if not isinstance(resource, dict) or resource.get("name") != "buy.browse":
                continue
            entries = resource.get("rates")
            if not isinstance(entries, list):
                raise AnalyticsError("Missing Browse quota rates")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise AnalyticsError("Invalid Browse quota rate")
                limit, remaining, window = (entry.get(k) for k in ("limit", "remaining", "timeWindow"))
                count = entry.get("count")
                if (any(type(v) is not int for v in (limit, remaining, window)) or limit <= 0
                        or remaining < 0 or remaining > limit or window <= 0
                        or (count is not None and (type(count) is not int or count < 0))):
                    raise AnalyticsError("Invalid Browse quota values")
                reset = None
                if isinstance(entry.get("reset"), str):
                    try:
                        stamp = datetime.fromisoformat(entry["reset"].replace("Z", "+00:00"))
                        if stamp.tzinfo is not None:
                            reset = stamp.timestamp()
                    except (ValueError, OverflowError):
                        pass
                rates.append(QuotaRate(limit, count, remaining, reset, window))
    if not rates:
        raise AnalyticsError("No applicable buy.browse quota returned")
    return BrowseQuota(tuple(rates))


async def get_browse_quota(client) -> BrowseQuota:
    import aiohttp

    refreshed = False
    retries = 0
    async with aiohttp.ClientSession() as session:
        while True:
            generation = client._token_generation
            status = None
            try:
                async with session.get(ANALYTICS_URL,
                                       headers={"Authorization": f"Bearer {client.token}", "Accept": "application/json"},
                                       params={"api_context": "buy", "api_name": "browse"},
                                       timeout=aiohttp.ClientTimeout(total=15)) as response:
                    status = response.status
                    if status == 200:
                        try:
                            data = await response.json()
                        except (ValueError, aiohttp.ContentTypeError):
                            raise AnalyticsError("Invalid Analytics JSON") from None
                        return parse_browse_quota(data)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                reason = f"Analytics transport error: {type(exc).__name__}"
            else:
                if status == 401 and not refreshed:
                    refreshed = True
                    if await client._refresh_token(generation):
                        continue
                if status not in (500, 502, 503, 504):
                    raise AnalyticsError(f"Analytics HTTP {status}")
                reason = f"Analytics HTTP {status}"
            if retries >= 2:
                raise AnalyticsError(reason)
            retries += 1
            await asyncio.sleep(retries)
