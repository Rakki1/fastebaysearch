"""Scheduling guidance from complete runs of the same search configuration."""
from __future__ import annotations

import hashlib
import json
import math

from .analytics import BrowseQuota

SAMPLE_LIMIT = 5
SAMPLE_MAX_AGE = 7 * 86400


def search_profile(config, queries) -> str:
    sites = sorted(set(config.ebay_sites))
    data = {
        "version": 1,
        "sites": sites,
        "queries": sorted(q.keywords for q in queries),
        "categories": {site: sorted(config.ebay_category_ids.get(site, [])) for site in sites},
        "excluded_categories": {site: sorted(config.ebay_exclude_category_ids.get(site, [])) for site in sites},
        "buying_options": sorted(config.ebay_buying_options),
        "description_search": config.ebay_search_in_description,
        "recommendations": config.ebay_filter_recommendations,
        "validation": config.ebay_validate_search_terms,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def scheduling_capacity(quota: BrowseQuota, calls: int, safety_percent: int):
    """Honor every returned rate window, including limits shorter than a day."""
    hard_intervals = []
    safe_intervals = []
    for rate in quota.rates:
        hard_runs = rate.limit // calls
        safe_runs = math.floor(rate.limit * safety_percent / 100) // calls
        hard_intervals.append(rate.time_window / hard_runs if hard_runs else float("inf"))
        safe_intervals.append(rate.time_window / safe_runs if safe_runs else float("inf"))
    hard_interval = max(hard_intervals)
    safe_interval = max(safe_intervals)
    theoretical = 0 if math.isinf(hard_interval) else math.floor(86400 / hard_interval)
    minutes = None if math.isinf(safe_interval) else max(1, math.ceil(safe_interval / 60))
    recommended = 0 if minutes is None else 1440 // minutes
    return theoretical, recommended, minutes


class RuntimeBudget:
    def __init__(self, store, config, queries):
        self.store = store
        self.profile = search_profile(config, queries)
        self.queries_count = len(queries)
        self.sites_count = len(config.ebay_sites)
        self.minimum_calls = sum(max(1, len(config.ebay_category_ids.get(site, [])))
                                 for site in config.ebay_sites) * len(queries)
        self.safety_percent = config.api_budget_safety_percent
        self.call_cap = config.ebay_max_search_calls_per_run

    def record(self, run, now):
        # Incomplete/capped runs understate the cost of a full search.
        verification_capped = run.recommendations and run.recommendations.budget_exhausted
        if run.exit_code == 0 and not verification_capped and type(run.browse_calls) is int and run.browse_calls > 0:
            self.store.record_run_cost(self.profile, run.browse_calls, now)

    def summary(self, quota: BrowseQuota | None, now: float) -> str:
        costs = self.store.run_costs(self.profile, now)
        heading = (f"Search budget: {self.queries_count} queries, {self.sites_count} marketplaces, "
                   f"at least {self.minimum_calls} search calls/run.")
        if not costs:
            return (heading + "\nFull-run cost has not been measured for this configuration. "
                    "A scheduling recommendation needs one complete run, including description calls and retries.")
        calls = max(costs)
        text = (heading + f"\nMeasured full-run cost: {calls} Browse calls "
                f"(maximum of {len(costs)} recent complete runs, up to {SAMPLE_LIMIT} within 7 days).")
        if quota is None:
            text += "\nActual quota unavailable; scheduling frequency cannot be calculated."
        else:
            theoretical, recommended, minutes = scheduling_capacity(quota, calls, self.safety_percent)
            text += f"\nTheoretical capacity: {theoretical} complete runs/24h."
            if minutes is None:
                text += (f"\nA complete run does not fit within the {self.safety_percent}% safety budget "
                         "of at least one quota window; no safe frequency can be recommended.")
            else:
                text += (f"\nRecommended: {recommended} complete runs/24h, interval at least {minutes} minutes "
                         f"({self.safety_percent}% quota budget).")
        if self.call_cap < calls:
            text += f"\nThe configured per-run cap {self.call_cap} is below the measured full-run cost {calls}."
        return text + "\nEstimate only: result counts and other applications using this quota can change."
