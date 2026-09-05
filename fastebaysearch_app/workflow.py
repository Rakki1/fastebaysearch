from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .config import AppConfig
from .database import Database
from .ebay_client import EbayClient
from .models import SearchQuery, SearchResult, SearchRun
from .notifications.email import EmailNotifier
from .notifications.html_report import HtmlReportNotifier
from .notifications.telegram import TelegramNotifier


@dataclass(frozen=True)
class WorkflowResult:
    all_found_items: list[SearchResult]
    unique_item_ids: list[SearchResult]
    new_results: list[SearchResult]
    search_run: SearchRun = field(default_factory=SearchRun)
    notification_errors: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return self.search_run.exit_code or int(bool(self.notification_errors))


def dedupe_by_item_id(results: list[SearchResult]) -> list[SearchResult]:
    deduped: dict[str, SearchResult] = {}
    for result in results:
        deduped[result.item_id] = result
    return list(deduped.values())


class Workflow:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        ebay_client: EbayClient,
        queries: list[SearchQuery],
        logger: logging.Logger | None = None,
        email_notifier=None,
        html_report_notifier=None,
        telegram_notifier=None,
    ):
        self.config = config
        self.database = database
        self.ebay_client = ebay_client
        self.queries = queries
        self.logger = logger or logging.getLogger("fastebaysearch")
        self.email_notifier = email_notifier
        self.html_report_notifier = html_report_notifier
        self.telegram_notifier = telegram_notifier

    async def run(self) -> WorkflowResult:
        rates = self._get_exchange_rates_from_cache()
        if not rates:
            rates = await self.ebay_client.get_exchange_rates()
            if rates and self.config.exchange_rate_cache_ttl_hours > 0:
                self.database.replace_exchange_rates(rates)
            elif self.config.exchange_rate_cache_ttl_hours > 0:
                rates = self.database.get_cached_exchange_rates()
                if rates:
                    self.logger.warning("Using stale cached exchange rates because live refresh failed.")

        search_run = await self.ebay_client.run_queries(self.config.ebay_sites, self.queries, rates)
        all_found_items = search_run.items
        unique_item_ids = dedupe_by_item_id(all_found_items)
        new_results = self.database.claim_new_results(unique_item_ids, enqueue_html=self.config.use_html_report)
        result = WorkflowResult(all_found_items, unique_item_ids, new_results, search_run)

        if self.config.use_html_report:
            person_name = self.config.email.person_name if self.config.email else "User"
            html_report = self.html_report_notifier or HtmlReportNotifier(
                self.config.html_report_dir,
                self.config.html_report_max_per_run,
                self.logger,
            )
            try:
                if not self.database.deliver_html_notifications(
                    html_report, self.config.html_report_max_per_run, person_name, search_run.summary
                ):
                    result.notification_errors.append("HTML report write failed")
            except Exception as exc:
                self.logger.error("HTML report notification failed: %s", type(exc).__name__)
                result.notification_errors.append("HTML report notification failed")

        if self.config.use_telegram and self.config.telegram:
            try:
                if not await self._notify_telegram(search_run.summary):
                    result.notification_errors.append("Telegram notification failed")
            except Exception as exc:
                self.logger.error("Telegram notification failed: %s", type(exc).__name__)
                result.notification_errors.append("Telegram notification failed")

        if self.config.use_email and self.config.email:
            try:
                if not self._notify_email(search_run.summary):
                    result.notification_errors.append("Email notification failed")
            except Exception as exc:
                self.logger.error("Email notification failed: %s", type(exc).__name__)
                result.notification_errors.append("Email notification failed")

        return result

    async def _notify_telegram(self, summary: str) -> bool:
        pending = self.database.claim_pending_telegram_notifications(self.config.telegram.max_per_run)
        if not pending:
            return True
        telegram = self.telegram_notifier or TelegramNotifier(self.config.telegram, self.logger)
        try:
            header_ok = await telegram.send_header(len(pending), run_summary=summary)
        except Exception:
            self.database.release_telegram_claims(pending, "Telegram header notification failed")
            raise
        if not header_ok:
            self.database.release_telegram_claims(pending, "Telegram header notification failed")
            return False
        try:
            sent = await telegram.send(pending)
        except Exception:
            self.database.mark_telegram_failed(pending, "Telegram notification failed")
            raise
        sent_ids = {item.item_id for item in sent}
        failed = [item for item in pending if item.item_id not in sent_ids]
        self.database.mark_telegram_succeeded(sent)
        self.database.mark_telegram_failed(failed, "Telegram notification failed")
        return not failed

    def _notify_email(self, summary: str) -> bool:
        pending = self.database.pending_email_notifications(self.config.email.max_per_run)
        if not pending:
            return True
        email = self.email_notifier or EmailNotifier(self.config.email, self.logger)
        try:
            sent = email.send(pending, run_summary=summary)
        except Exception:
            self.database.mark_email_failed(pending, "Email notification failed")
            raise
        sent_ids = {item.item_id for item in sent}
        failed = [item for item in pending if item.item_id not in sent_ids]
        self.database.mark_email_succeeded(sent)
        self.database.mark_email_failed(failed, "Email notification failed")
        return not failed

    def _get_exchange_rates_from_cache(self) -> dict[str, float]:
        if self.config.exchange_rate_cache_ttl_hours <= 0:
            return {}
        max_age_seconds = self.config.exchange_rate_cache_ttl_hours * 60 * 60
        rates = self.database.get_cached_exchange_rates(max_age_seconds)
        if rates:
            self.logger.info("Using cached exchange rates.")
        return rates


def log_summary(logger: logging.Logger, result: WorkflowResult, start_time: float) -> None:
    logger.info(result.search_run.summary)
    for error in result.notification_errors:
        logger.error(error)
    logger.info(f"Total raw results: {len(result.all_found_items)}")
    logger.info(f"Unique item IDs: {len(result.unique_item_ids)}")
    logger.info(f"New items found: {len(result.new_results)}")
    logger.info(
        f"Finished with exit_code={result.exit_code}. Items searched: {len(result.all_found_items)} | "
        f"Unique item IDs: {len(result.unique_item_ids)} | "
        f"New items: {len(result.new_results)} | "
        f"Duration: {time.time() - start_time:.2f}s"
    )
