import asyncio
import json
import logging

import pytest

from fastebaysearch_app import cli
from fastebaysearch_app.models import SearchResult, SearchRun


def minimal_config():
    return {
        "use_email": False,
        "use_telegram": False,
        "use_html_report": False,
        "html_report_dir": "reports",
        "html_report_max_per_run": 1,
        "ebay_sites": ["EBAY_US"],
        "exclude_terms": [],
        "ebay_client_id": "client-id",
        "ebay_client_secret": "client-secret",
        "ebay_urls_dbfile": "items.db",
        "ebay_oauth_file": "oauth_token.json",
        "ebay_search_keywords": {
            "base_terms": ["camera"],
            "required_terms": [],
        },
        "log_to_console": False,
    }


class FakeEbayAuth:
    def __init__(self, token_file, client_id, client_secret, logger):
        self.token_file = token_file
        self.client_id = client_id
        self.client_secret = client_secret
        self.logger = logger

    def get_access_token(self):
        return "token"


class FakeEbayClient:
    instances = []

    def __init__(self, token, logger, concurrency_limit, buying_options=None, auth=None):
        self.token = token
        self.logger = logger
        self.concurrency_limit = concurrency_limit
        self.exchange_rate_calls = 0
        self.run_query_calls = []
        FakeEbayClient.instances.append(self)

    async def get_exchange_rates(self):
        self.exchange_rate_calls += 1
        return {"USD": 1.2}

    async def run_queries(self, sites, queries, rates):
        self.run_query_calls.append((sites, queries, rates))
        return SearchRun(items=[
            SearchResult("camera", "Camera", "EBAY_US", "10 EUR", "1", "https://example.com/1", "seller", "", ""),
            SearchResult("camera", "Camera duplicate", "EBAY_US", "10 EUR", "1", "https://example.com/1", "seller", "", ""),
            SearchResult("camera", "Lens", "EBAY_US", "20 EUR", "2", "https://example.com/2", "seller", "", ""),
        ])


class FakeHtmlReportNotifier:
    instances = []

    def __init__(self, report_dir, max_per_run, logger):
        self.report_dir = report_dir
        self.max_per_run = max_per_run
        self.logger = logger
        self.sent_results = []
        self.person_name = None
        FakeHtmlReportNotifier.instances.append(self)

    def send(self, results, person_name="User", run_summary=""):
        self.sent_results = list(results[: self.max_per_run])
        self.person_name = person_name
        return self.sent_results


class FailingHtmlReportNotifier(FakeHtmlReportNotifier):
    def send(self, results, person_name="User", run_summary=""):
        return []


def write_config(tmp_path, data=None):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data or minimal_config()), encoding="utf-8")
    return config_path


def setup_clean_search_fakes(monkeypatch, report_notifier=FakeHtmlReportNotifier):
    FakeEbayClient.instances = []
    FakeHtmlReportNotifier.instances = []
    monkeypatch.setattr(cli, "EbayAuth", FakeEbayAuth)
    monkeypatch.setattr(cli, "EbayClient", FakeEbayClient)
    monkeypatch.setattr(cli, "HtmlReportNotifier", report_notifier)
    monkeypatch.setattr(
        cli,
        "setup_logging",
        lambda path, log_to_console, log_max_size_mb: logging.getLogger("test-clean-search"),
    )


def test_parser_rejects_estimate_api_budget_with_clean_search():
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["config.json", "--estimate-api-budget", "--clean-search"])


def test_clean_search_writes_html_report_without_database_or_enabled_html_config(tmp_path, monkeypatch):
    config_path = write_config(tmp_path)
    setup_clean_search_fakes(monkeypatch)
    monkeypatch.setattr(cli, "Database", lambda *args, **kwargs: pytest.fail("Database must not be used"))

    result = asyncio.run(cli.run_clean_search(config_path, tmp_path))

    assert result == 1  # The clean report limit intentionally omits one unique result.
    assert len(FakeEbayClient.instances) == 1
    assert FakeEbayClient.instances[0].exchange_rate_calls == 1
    assert len(FakeHtmlReportNotifier.instances) == 1
    report = FakeHtmlReportNotifier.instances[0]
    assert report.max_per_run == 1
    assert [item.item_id for item in report.sent_results] == ["1"]
    assert report.person_name == "User"


def test_clean_search_report_failure_returns_error(tmp_path, monkeypatch):
    config_path = write_config(tmp_path)
    setup_clean_search_fakes(monkeypatch, FailingHtmlReportNotifier)

    result = asyncio.run(cli.run_clean_search(config_path, tmp_path))

    assert result == 1


@pytest.mark.parametrize("runner", [cli.run_app, cli.run_clean_search, cli.run_api_budget_estimate])
def test_invalid_query_fails_before_logging_auth_or_database(tmp_path, monkeypatch, capsys, runner):
    import inspect
    data = minimal_config()
    data["ebay_search_keywords"]["base_terms"] = ["x" * 101]
    config_path = write_config(tmp_path, data)

    def forbidden(*args, **kwargs):
        pytest.fail("Query validation must happen before any side effects")

    for name in ("EbayAuth", "Database", "setup_logging"):
        monkeypatch.setattr(cli, name, forbidden)
    code = runner(config_path, tmp_path)
    if inspect.isawaitable(code):
        code = asyncio.run(code)
    assert code == 1
    assert "101 characters" in capsys.readouterr().err
    assert not (tmp_path / "items.db").exists()


def test_budget_estimate_is_offline(tmp_path, monkeypatch, capsys):
    config_path = write_config(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("Budget estimation must not open database, network, or log")

    for name in ("EbayAuth", "EbayClient", "Database", "setup_logging"):
        monkeypatch.setattr(cli, name, forbidden)
    assert cli.run_api_budget_estimate(config_path, tmp_path) == 0
    assert "OAuth calls (separate from Browse)" in capsys.readouterr().out


@pytest.mark.parametrize("runner", [cli.run_app, cli.run_clean_search])
@pytest.mark.parametrize("status,expected", [("success", 0), ("failed", 1), ("truncated", 1), ("rate_limited", 2)])
def test_exit_codes_and_partial_results(tmp_path, monkeypatch, runner, status, expected):
    from fastebaysearch_app.models import QueryOutcome, SearchQuery
    from fastebaysearch_app.notifications.html_report import HtmlReportNotifier
    from fastebaysearch_app.database import Database
    data = minimal_config()
    data.update(use_html_report=True, html_report_max_per_run=10, exchange_rate_cache_ttl_hours=0)
    monkeypatch.chdir(tmp_path)
    config_path = write_config(tmp_path, data)
    setup_clean_search_fakes(monkeypatch, HtmlReportNotifier)
    query = SearchQuery("camera", "camera")

    async def results(self, *args):
        return SearchRun(items=[SearchResult("camera", "Camera", "EBAY_GB", "10 GBP", "12345",
                         "https://www.ebay.co.uk/itm/12345", "seller", "", "")],
                         outcomes=[QueryOutcome("EBAY_GB", query, status=status)])

    monkeypatch.setattr(FakeEbayClient, "run_queries", results)
    if runner is cli.run_clean_search:
        monkeypatch.setattr(cli, "Database", lambda *a, **kw: pytest.fail("Clean search used DB"))
    assert asyncio.run(runner(config_path, tmp_path)) == expected
    reports = list((tmp_path / "reports").glob("*.html"))
    assert len(reports) == 1
    assert ("PARTIAL SEARCH" in reports[0].read_text()) is (expected != 0)
    if runner is cli.run_app:
        with Database(tmp_path / "items.db").connect() as conn:
            assert conn.execute("SELECT item_id FROM ebayids").fetchall() == [("12345",)]
            assert conn.execute("SELECT delivered_at FROM html_notifications").fetchone()[0]
            assert conn.execute("SELECT end_time FROM history WHERE run_number=1").fetchone()[0]
    else:
        assert not (tmp_path / "items.db").exists()


def test_rate_limit_wins_over_report_failure(tmp_path, monkeypatch):
    from fastebaysearch_app.models import QueryOutcome, SearchQuery
    config_path = write_config(tmp_path)
    setup_clean_search_fakes(monkeypatch, FailingHtmlReportNotifier)

    async def results(self, *args):
        return SearchRun(items=[SearchResult("camera", "Camera", "EBAY_GB", "10 GBP", "12345", "", "", "", "")],
                         outcomes=[QueryOutcome("EBAY_GB", SearchQuery("camera", "camera"), status="rate_limited")])

    monkeypatch.setattr(FakeEbayClient, "run_queries", results)
    assert asyncio.run(cli.run_clean_search(config_path, tmp_path)) == 2
