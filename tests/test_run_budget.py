import asyncio
from dataclasses import replace

import pytest

from fastebaysearch_app.analytics import BrowseQuota, QuotaRate
from fastebaysearch_app.models import SearchRun, RecommendationStats
from fastebaysearch_app.query_builder import build_queries
from fastebaysearch_app.quota_tracking import QuotaStore
from fastebaysearch_app.run_budget import RuntimeBudget, SAMPLE_MAX_AGE, scheduling_capacity, search_profile
from fastebaysearch_app.config import load_config
from test_query_builder import make_config
from test_quota_tracking import Client, Telegram, completed, config, quota, set_clock, NOW
from fastebaysearch_app import workflow
from fastebaysearch_app.database import Database


def measured_budget(tmp_path):
    cfg = make_config()
    database = Database(tmp_path / "items.db")
    database.ensure_schema()
    store = QuotaStore(database.connect, cfg.ebay_client_id)
    return RuntimeBudget(store, cfg, build_queries(cfg))


def test_actual_541_call_run_supports_eight_runs_with_safety_margin():
    assert scheduling_capacity(quota(), 541, 90) == (9, 8, 180)
    assert scheduling_capacity(quota(), 541, 100) == (9, 9, 160)
    # Shorter rate windows can be the binding constraint, even with a large daily quota.
    limits = BrowseQuota((QuotaRate(5000, 0, 5000, NOW + 86400, 86400),
                          QuotaRate(600, 0, 600, NOW + 14400, 14400)))
    assert scheduling_capacity(limits, 541, 100) == (6, 6, 240)


def test_run_that_does_not_fit_has_no_misleading_interval():
    assert scheduling_capacity(quota(), 6000, 90) == (0, 0, None)
    assert scheduling_capacity(quota(), 4800, 90) == (1, 0, None)
    assert scheduling_capacity(quota(), 541, 0) == (9, 0, None)


def test_budget_includes_actual_calls_and_warns_about_too_small_cap(tmp_path):
    budget = measured_budget(tmp_path)
    budget.record(SearchRun(browse_calls=541), NOW)
    summary = budget.summary(quota(), NOW + 10)
    assert "541 Browse calls" in summary
    assert "9 complete runs/24h" in summary
    assert "Recommended: 8 complete runs/24h" in summary
    assert "180 minutes" in summary
    assert "cap 500 is below" in summary
    assert "Estimate only" in summary


def test_recent_maximum_is_used_instead_of_optimistic_average(tmp_path):
    budget = measured_budget(tmp_path)
    for i, calls in enumerate([700, 510, 541, 530, 520]):
        budget.record(SearchRun(browse_calls=calls), NOW + i)
    assert "700 Browse calls" in budget.summary(quota(), NOW + 10)
    budget.record(SearchRun(browse_calls=500), NOW + 11)
    assert "541 Browse calls" in budget.summary(quota(), NOW + 12)
    assert "700 Browse calls" not in budget.summary(quota(), NOW + 12)


@pytest.mark.parametrize("run", [
    SearchRun(browse_calls=0), SearchRun(), SearchRun(browse_calls=True),
    SearchRun(browse_calls=500, recommendations=RecommendationStats(deferred=41)),
    SearchRun(browse_calls=500, recommendations=RecommendationStats(uncertain=41, budget_exhausted=True)),
    SearchRun(browse_calls=300, quota_blocked=True),
    replace(completed("truncated"), browse_calls=500),
    replace(completed("failed"), browse_calls=2),
])
def test_partial_or_unmeasured_runs_do_not_understate_full_cost(tmp_path, run):
    budget = measured_budget(tmp_path)
    budget.record(run, NOW)
    summary = budget.summary(quota(), NOW)
    assert "has not been measured" in summary
    assert "Recommended:" not in summary


def test_old_measurements_and_other_applications_are_not_used(tmp_path):
    budget = measured_budget(tmp_path)
    budget.record(SearchRun(browse_calls=541), NOW)
    assert "has not been measured" in budget.summary(quota(), NOW + SAMPLE_MAX_AGE + 1)
    different_app = QuotaStore(budget.store.connect, "other-app")
    assert different_app.run_costs(budget.profile, NOW) == []


def test_changed_search_configuration_does_not_reuse_previous_measurement(tmp_path):
    budget = measured_budget(tmp_path)
    budget.record(SearchRun(browse_calls=541), NOW)
    cfg = make_config()
    cfg.search.base_terms[:] = ["another title"]
    changed = RuntimeBudget(budget.store, cfg, build_queries(cfg))
    assert changed.profile != budget.profile
    assert "has not been measured" in changed.summary(quota(), NOW)
    original = make_config()
    old_hash = search_profile(original, build_queries(original))
    for changed in [replace(original, ebay_search_in_description=False),
                    replace(original, ebay_filter_recommendations=True),
                    replace(original, ebay_validate_search_terms=True),
                    replace(original, ebay_buying_options=["AUCTION"]),
                    replace(original, ebay_category_ids={"EBAY_US": ["1249"]}),
                    replace(original, ebay_exclude_category_ids={"EBAY_US": ["42"]})]:
        assert search_profile(changed, build_queries(changed)) != old_hash
    # Caps and delivery settings do not change the intrinsic cost of a complete search.
    assert search_profile(replace(original, ebay_max_search_calls_per_run=1000), build_queries(original)) == old_hash


def test_missing_live_quota_keeps_cost_but_does_not_guess_frequency(tmp_path):
    budget = measured_budget(tmp_path)
    budget.record(SearchRun(browse_calls=541), NOW)
    summary = budget.summary(None, NOW)
    assert "541 Browse calls" in summary
    assert "Actual quota unavailable" in summary
    assert "Recommended:" not in summary


def test_current_collector_configuration_has_78_search_combinations(tmp_path):
    from pathlib import Path
    cfg = load_config(Path(__file__).parent / "fixtures/collector_search.json", tmp_path)
    database = Database(tmp_path / "items.db")
    database.ensure_schema()
    budget = RuntimeBudget(QuotaStore(database.connect, cfg.ebay_client_id), cfg, build_queries(cfg))
    summary = budget.summary(quota(), NOW)
    assert "13 queries, 6 marketplaces, at least 78 search calls/run" in summary


def test_completed_search_measurement_appears_in_next_single_telegram_limit_warning(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    database = Database(cfg.db_path)
    database.ensure_schema()
    now = [NOW]
    set_clock(monkeypatch, now)
    queries = build_queries(cfg)
    client = Client([quota(remaining=4999), quota()], [replace(completed(), browse_calls=541)])
    telegram = Telegram()

    def run():
        return asyncio.run(workflow.Workflow(cfg, database, client, queries, telegram_notifier=telegram).run())

    assert run().exit_code == 0
    now[0] += 1800
    assert run().exit_code == 2
    assert run().exit_code == 2
    assert len(telegram.messages) == 1
    text = telegram.messages[0]
    assert "Search budget:" in text
    assert "541 Browse calls" in text
    assert "8 complete runs/24h" in text
    assert "180 minutes" in text
    assert "resume" in text
    assert len(text) < 3500


def test_real_client_counts_search_and_description_attempts_together(monkeypatch):
    import aiohttp
    from fastebaysearch_app.ebay_client import EbayClient
    from test_search_reliability import Response, Session
    from test_recommendation_filter import query, item, HTML, REST_ID
    session = Session([Response(data={"total": 1, "itemSummaries": [item()]}),
                       Response(data={"itemId": REST_ID, "description": HTML + "<p>Anirog C64</p>"})])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    client = EbayClient("token", validate_search_terms=True)
    result = asyncio.run(client.run_queries(["EBAY_GB"], [query()], {}))
    assert result.exit_code == 0
    assert result.browse_calls == 2
    assert client.description_calls == 1


def test_legacy_recommendation_cap_is_not_a_complete_budget_measurement(monkeypatch, tmp_path):
    import aiohttp
    from fastebaysearch_app.ebay_client import EbayClient
    from test_search_reliability import Response, Session
    from test_recommendation_filter import query, item
    session = Session([Response(data={"total": 1, "itemSummaries": [item()]})])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    client = EbayClient("token", filter_recommendations=True, max_search_calls=1)
    result = asyncio.run(client.run_queries(["EBAY_GB"], [query()], {}))
    assert result.exit_code == 0  # Legacy mode retains uncertain items.
    assert result.recommendations.budget_exhausted
    budget = measured_budget(tmp_path)
    budget.record(result, NOW)
    assert "has not been measured" in budget.summary(quota(), NOW)
