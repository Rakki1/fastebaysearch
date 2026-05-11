import asyncio
from pathlib import Path

from fastebaysearch_app.config import AppConfig, EmailConfig, SearchConfig, TelegramConfig
from fastebaysearch_app.models import SearchQuery, SearchResult
from fastebaysearch_app.workflow import Workflow


class FakeEbayClient:
    def __init__(self):
        self.exchange_rate_calls = 0

    async def get_exchange_rates(self):
        self.exchange_rate_calls += 1
        return {"USD": 1.2}

    async def run_queries(self, sites, queries, rates):
        return [
            SearchResult("camera", "Camera", "EBAY_US", "10 EUR", "1", "https://example.com", "seller", "", ""),
        ]


class FakeDatabase:
    def __init__(self):
        self.result = SearchResult("camera", "Camera", "EBAY_US", "10 EUR", "1", "https://example.com", "seller", "", "")
        self.second_result = SearchResult("camera", "Lens", "EBAY_US", "20 EUR", "2", "https://example.com/2", "seller", "", "")
        self.telegram_succeeded = False
        self.email_succeeded = False
        self.telegram_success_ids = []
        self.email_success_ids = []
        self.telegram_failed_ids = []
        self.email_failed_ids = []
        self.cached_rates = {}
        self.replaced_rates = None

    def claim_new_results(self, results):
        return results

    def pending_telegram_notifications(self, limit=None):
        return [self.result, self.second_result][:limit]

    def pending_email_notifications(self, limit=None):
        return [self.result, self.second_result][:limit]

    def get_cached_exchange_rates(self, max_age_seconds=None):
        return self.cached_rates

    def replace_exchange_rates(self, rates):
        self.replaced_rates = rates

    def mark_telegram_succeeded(self, results):
        self.telegram_succeeded = True
        self.telegram_success_ids = [result.item_id for result in results]

    def mark_telegram_failed(self, results, error):
        self.telegram_failed_ids = [result.item_id for result in results]

    def mark_email_succeeded(self, results):
        self.email_succeeded = True
        self.email_success_ids = [result.item_id for result in results]

    def mark_email_failed(self, results, error):
        self.email_failed_ids = [result.item_id for result in results]


class FakeTelegramNotifier:
    async def send_header(self, count):
        return True

    async def send(self, results):
        return results[:1]


class FakeEmailNotifier:
    def send(self, results):
        return results[:1]


class FakeHtmlReportNotifier:
    def __init__(self):
        self.sent_results = []
        self.person_name = None

    def send(self, results, person_name="User"):
        self.sent_results = list(results)
        self.person_name = person_name
        return results


class FailingHtmlReportNotifier:
    def send(self, results, person_name="User"):
        raise RuntimeError("write failed")


def make_config(use_email=True, use_telegram=True, use_html_report=False):
    return AppConfig(
        db_path=None,
        token_file=None,
        ebay_client_id="client-id",
        ebay_client_secret="client-secret",
        use_email=use_email,
        use_telegram=use_telegram,
        use_html_report=use_html_report,
        html_report_dir=Path("."),
        html_report_max_per_run=1000,
        ebay_sites=["EBAY_US"],
        exclude_terms=[],
        search=SearchConfig(base_terms=["camera"], required_terms=[]),
        email=(
            EmailConfig(
                smtp_server="smtp.example.com",
                smtp_port=587,
                smtp_login="user",
                smtp_password="password",
                sender="from@example.com",
                receiver="to@example.com",
                subject="Subject",
                person_name="User",
            )
            if use_email
            else None
        ),
        telegram=TelegramConfig(token="token", chat_id="chat", max_per_run=2) if use_telegram else None,
        log_to_console=False,
    )


def test_workflow_uses_injected_notifiers_without_sleep_or_network():
    database = FakeDatabase()
    ebay_client = FakeEbayClient()
    workflow = Workflow(
        make_config(),
        database,
        ebay_client,
        [SearchQuery("camera", "camera")],
        email_notifier=FakeEmailNotifier(),
        telegram_notifier=FakeTelegramNotifier(),
    )

    result = asyncio.run(workflow.run())

    assert len(result.new_results) == 1
    assert database.telegram_succeeded is True
    assert database.email_succeeded is True
    assert database.telegram_success_ids == ["1"]
    assert database.telegram_failed_ids == ["2"]
    assert database.email_success_ids == ["1"]
    assert database.email_failed_ids == ["2"]
    assert ebay_client.exchange_rate_calls == 1
    assert database.replaced_rates == {"USD": 1.2}


def test_workflow_uses_cached_exchange_rates():
    database = FakeDatabase()
    database.cached_rates = {"USD": 1.2}
    ebay_client = FakeEbayClient()
    workflow = Workflow(
        make_config(),
        database,
        ebay_client,
        [SearchQuery("camera", "camera")],
        email_notifier=FakeEmailNotifier(),
        telegram_notifier=FakeTelegramNotifier(),
    )

    asyncio.run(workflow.run())

    assert ebay_client.exchange_rate_calls == 0
    assert database.replaced_rates is None


def test_workflow_can_write_html_report_without_email_or_telegram():
    database = FakeDatabase()
    ebay_client = FakeEbayClient()
    html_report = FakeHtmlReportNotifier()
    workflow = Workflow(
        make_config(use_email=False, use_telegram=False, use_html_report=True),
        database,
        ebay_client,
        [SearchQuery("camera", "camera")],
        html_report_notifier=html_report,
    )

    asyncio.run(workflow.run())

    assert [result.item_id for result in html_report.sent_results] == ["1"]
    assert html_report.person_name == "User"
    assert database.email_succeeded is False
    assert database.telegram_succeeded is False


def test_workflow_can_run_html_report_with_email_and_telegram():
    database = FakeDatabase()
    ebay_client = FakeEbayClient()
    html_report = FakeHtmlReportNotifier()
    workflow = Workflow(
        make_config(use_html_report=True),
        database,
        ebay_client,
        [SearchQuery("camera", "camera")],
        email_notifier=FakeEmailNotifier(),
        html_report_notifier=html_report,
        telegram_notifier=FakeTelegramNotifier(),
    )

    asyncio.run(workflow.run())

    assert [result.item_id for result in html_report.sent_results] == ["1"]
    assert html_report.person_name == "User"
    assert database.telegram_succeeded is True
    assert database.email_succeeded is True


def test_workflow_html_report_failure_does_not_block_other_channels():
    database = FakeDatabase()
    ebay_client = FakeEbayClient()
    workflow = Workflow(
        make_config(use_html_report=True),
        database,
        ebay_client,
        [SearchQuery("camera", "camera")],
        email_notifier=FakeEmailNotifier(),
        html_report_notifier=FailingHtmlReportNotifier(),
        telegram_notifier=FakeTelegramNotifier(),
    )

    asyncio.run(workflow.run())

    assert database.telegram_succeeded is True
    assert database.email_succeeded is True
