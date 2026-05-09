from fastebaysearch_app.api_budget import estimate_api_budget, format_api_budget_estimate
from fastebaysearch_app.models import SearchQuery


def test_estimate_api_budget_uses_sites_queries_and_pagination():
    queries = [SearchQuery("camera", "camera"), SearchQuery("lens", "lens")]

    estimate = estimate_api_budget(
        ["EBAY_US", "EBAY_GB", "EBAY_DE"],
        queries,
        daily_api_limit=10000,
        safety_percent=90,
        estimated_results_per_query=450,
    )

    assert estimate.generated_queries_count == 2
    assert estimate.ebay_sites_count == 3
    assert estimate.estimated_pages_per_query == 3
    assert estimate.calls_per_run_minimum == 6
    assert estimate.calls_per_run_estimated == 18
    assert estimate.safe_daily_api_limit == 9000
    assert estimate.possible_runs_per_day_estimated == 500
    assert estimate.recommended_interval_minutes_estimated == 3


def test_format_api_budget_estimate_includes_usage_values():
    estimate = estimate_api_budget(
        ["EBAY_US"],
        [SearchQuery("camera", "camera")],
        daily_api_limit=100,
        safety_percent=50,
        estimated_results_per_query=0,
    )

    output = format_api_budget_estimate(estimate)

    assert "eBay API budget estimate" in output
    assert "API calls per run, minimum: 1" in output
    assert "Safe daily limit: 50" in output
