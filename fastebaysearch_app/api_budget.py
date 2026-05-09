from __future__ import annotations

import math
from dataclasses import dataclass

from .ebay_client import EBAY_MAX_OFFSET
from .models import SearchQuery

DEFAULT_SEARCH_LIMIT = 200


@dataclass(frozen=True)
class ApiBudgetEstimate:
    ebay_sites_count: int
    generated_queries_count: int
    search_limit: int
    estimated_results_per_query: int
    estimated_pages_per_query: int
    calls_per_run_minimum: int
    calls_per_run_estimated: int
    daily_api_limit: int
    safe_daily_api_limit: int
    possible_runs_per_day_minimum: int
    possible_runs_per_day_estimated: int
    recommended_interval_minutes_estimated: int | None


def estimate_api_budget(
    ebay_sites: list[str],
    queries: list[SearchQuery],
    daily_api_limit: int = 10000,
    safety_percent: int = 90,
    estimated_results_per_query: int = 200,
    search_limit: int = DEFAULT_SEARCH_LIMIT,
) -> ApiBudgetEstimate:
    sites_count = len(ebay_sites)
    queries_count = len(queries)
    combinations = sites_count * queries_count
    safe_daily_limit = math.floor(max(0, daily_api_limit) * max(0, min(100, safety_percent)) / 100)

    estimated_pages = _estimated_pages_per_query(estimated_results_per_query, search_limit)
    calls_minimum = combinations
    calls_estimated = combinations * estimated_pages

    runs_minimum = _runs_per_day(safe_daily_limit, calls_minimum)
    runs_estimated = _runs_per_day(safe_daily_limit, calls_estimated)
    interval = math.ceil(1440 / runs_estimated) if runs_estimated > 0 else None

    return ApiBudgetEstimate(
        ebay_sites_count=sites_count,
        generated_queries_count=queries_count,
        search_limit=search_limit,
        estimated_results_per_query=max(0, int(estimated_results_per_query)),
        estimated_pages_per_query=estimated_pages,
        calls_per_run_minimum=calls_minimum,
        calls_per_run_estimated=calls_estimated,
        daily_api_limit=max(0, int(daily_api_limit)),
        safe_daily_api_limit=safe_daily_limit,
        possible_runs_per_day_minimum=runs_minimum,
        possible_runs_per_day_estimated=runs_estimated,
        recommended_interval_minutes_estimated=interval,
    )


def format_api_budget_estimate(estimate: ApiBudgetEstimate) -> str:
    interval = (
        f"every {estimate.recommended_interval_minutes_estimated} minutes or slower"
        if estimate.recommended_interval_minutes_estimated is not None
        else "not available with the current estimate"
    )
    return "\n".join(
        [
            "eBay API budget estimate",
            f"eBay sites: {estimate.ebay_sites_count}",
            f"Generated queries: {estimate.generated_queries_count}",
            f"Search result limit per API call: {estimate.search_limit}",
            f"Estimated results per query: {estimate.estimated_results_per_query}",
            f"Estimated pages per query: {estimate.estimated_pages_per_query}",
            f"API calls per run, minimum: {estimate.calls_per_run_minimum}",
            f"API calls per run, estimated: {estimate.calls_per_run_estimated}",
            f"Daily API limit: {estimate.daily_api_limit}",
            f"Safe daily limit: {estimate.safe_daily_api_limit}",
            f"Possible runs per day, minimum: {estimate.possible_runs_per_day_minimum}",
            f"Possible runs per day, estimated: {estimate.possible_runs_per_day_estimated}",
            f"Recommended cron interval: {interval}",
        ]
    )


def _estimated_pages_per_query(estimated_results_per_query: int, search_limit: int) -> int:
    if search_limit < 1:
        raise ValueError("search_limit must be at least 1")
    max_pages = EBAY_MAX_OFFSET // search_limit + 1
    pages = max(1, math.ceil(max(0, int(estimated_results_per_query)) / search_limit))
    return min(pages, max_pages)


def _runs_per_day(safe_daily_limit: int, calls_per_run: int) -> int:
    if calls_per_run < 1:
        return 0
    return safe_daily_limit // calls_per_run
