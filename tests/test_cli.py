import asyncio
import json
import logging

import pytest

from fastebaysearch_app import cli
from fastebaysearch_app.models import SearchResult


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

    def __init__(self, token, logger, concurrency_limit):
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
        return [
            SearchResult("camera", "Camera", "EBAY_US", "10 EUR", "1", "https://example.com/1", "seller", "", ""),
            SearchResult("camera", "Camera duplicate", "EBAY_US", "10 EUR", "1", "https://example.com/1", "seller", "", ""),
            SearchResult("camera", "Lens", "EBAY_US", "20 EUR", "2", "https://example.com/2", "seller", "", ""),
        ]


class FakeHtmlReportNotifier:
    instances = []

    def __init__(self, report_dir, max_per_run, logger):
        self.report_dir = report_dir
        self.max_per_run = max_per_run
        self.logger = logger
        self.sent_results = []
        self.person_name = None
        FakeHtmlReportNotifier.instances.append(self)

    def send(self, results, person_name="User"):
        self.sent_results = list(results[: self.max_per_run])
        self.person_name = person_name
        return self.sent_results


class FailingHtmlReportNotifier(FakeHtmlReportNotifier):
    def send(self, results, person_name="User"):
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
    monkeypatch.setattr(cli, "setup_logging", lambda path, log_to_console: logging.getLogger("test-clean-search"))


def test_parser_rejects_estimate_api_budget_with_clean_search():
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["config.json", "--estimate-api-budget", "--clean-search"])


def test_clean_search_writes_html_report_without_database_or_enabled_html_config(tmp_path, monkeypatch):
    config_path = write_config(tmp_path)
    setup_clean_search_fakes(monkeypatch)
    monkeypatch.setattr(cli, "Database", lambda *args, **kwargs: pytest.fail("Database must not be used"))

    result = asyncio.run(cli.run_clean_search(config_path, tmp_path))

    assert result == 0
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
