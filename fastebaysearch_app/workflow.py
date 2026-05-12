from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .config import AppConfig
from .database import Database
from .ebay_client import EbayClient
from .models import SearchQuery, SearchResult
from .notifications.email import EmailNotifier
from .notifications.html_report import HtmlReportNotifier
from .notifications.telegram import TelegramNotifier


@dataclass(frozen=True)
class WorkflowResult:
    all_found_items: list[SearchResult]
    unique_item_ids: list[SearchResult]
    new_results: list[SearchResult]


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

        all_found_items = await self.ebay_client.run_queries(self.config.ebay_sites, self.queries, rates)
        unique_item_ids = dedupe_by_item_id(all_found_items)
        new_results = self.database.claim_new_results(unique_item_ids)

        if self.config.use_html_report and new_results:
            person_name = self.config.email.person_name if self.config.email else "User"
            html_report = self.html_report_notifier or HtmlReportNotifier(
                self.config.html_report_dir,
                self.config.html_report_max_per_run,
                self.logger,
            )
            try:
                html_report.send(new_results, person_name)
            except Exception as exc:
                self.logger.error(f"HTML report notification failed: {exc}")

        if self.config.use_telegram and self.config.telegram:
            pending_telegram = self.database.claim_pending_telegram_notifications(self.config.telegram.max_per_run)
            telegram = self.telegram_notifier or TelegramNotifier(self.config.telegram, self.logger)
            if pending_telegram:
                header_ok = await telegram.send_header(len(pending_telegram))
                if header_ok:
                    sent_telegram = await telegram.send(pending_telegram)
                    sent_telegram_ids = {sent.item_id for sent in sent_telegram}
                    failed_telegram = [result for result in pending_telegram if result.item_id not in sent_telegram_ids]
                    self.database.mark_telegram_succeeded(sent_telegram)
                    self.database.mark_telegram_failed(failed_telegram, "Telegram notification failed")
                else:
                    self.database.release_telegram_claims(pending_telegram, "Telegram header notification failed")

        if self.config.use_email and self.config.email:
            pending_email = self.database.pending_email_notifications(self.config.email.max_per_run)
            email = self.email_notifier or EmailNotifier(self.config.email, self.logger)
            if pending_email:
                sent_email = email.send(pending_email)
                sent_email_ids = {sent.item_id for sent in sent_email}
                failed_email = [result for result in pending_email if result.item_id not in sent_email_ids]
                self.database.mark_email_succeeded(sent_email)
                self.database.mark_email_failed(failed_email, "Email notification failed")

        return WorkflowResult(all_found_items, unique_item_ids, new_results)

    def _get_exchange_rates_from_cache(self) -> dict[str, float]:
        if self.config.exchange_rate_cache_ttl_hours <= 0:
            return {}
        max_age_seconds = self.config.exchange_rate_cache_ttl_hours * 60 * 60
        rates = self.database.get_cached_exchange_rates(max_age_seconds)
        if rates:
            self.logger.info("Using cached exchange rates.")
        return rates


def log_summary(logger: logging.Logger, result: WorkflowResult, start_time: float) -> None:
    logger.info(f"Total raw results: {len(result.all_found_items)}")
    logger.info(f"Unique item IDs: {len(result.unique_item_ids)}")
    logger.info(f"New items found: {len(result.new_results)}")
    logger.info(
        f"Completed. Items searched: {len(result.all_found_items)} | "
        f"Unique item IDs: {len(result.unique_item_ids)} | "
        f"New items: {len(result.new_results)} | "
        f"Duration: {time.time() - start_time:.2f}s"
    )
