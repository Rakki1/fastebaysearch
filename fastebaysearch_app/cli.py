from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from pathlib import Path

from .api_budget import estimate_api_budget, format_api_budget_estimate
from .config import ConfigError, load_config
from .database import Database
from .ebay_client import EbayAuth, EbayClient
from .logging_setup import setup_logging
from .notifications.html_report import HtmlReportNotifier
from .query_builder import build_queries
from .workflow import Workflow, WorkflowResult, dedupe_by_item_id, log_summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fastebaysearch.py")
    parser.add_argument("config", help="Path to JSON config file")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--estimate-api-budget", action="store_true",
                      help="Estimate API usage without making API calls")
    mode.add_argument("--clean-search", action="store_true",
                      help="Search without database access; write a local HTML report")
    return parser


def run_api_budget_estimate(config_path: Path, script_dir: Path) -> int:
    try:
        config = load_config(config_path, script_dir)
        queries = build_queries(config)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    estimate = estimate_api_budget(
        config.ebay_sites, queries, daily_api_limit=config.ebay_daily_api_limit,
        safety_percent=config.api_budget_safety_percent,
        estimated_results_per_query=config.estimated_results_per_query,
    )
    print(format_api_budget_estimate(estimate))
    return 0


def _log_queries(logger, queries) -> None:
    logger.info("Generated %s validated queries.", len(queries))
    for idx, query in enumerate(queries, start=1):
        logger.info("  %s. (%s/100 chars) Base: %s -> %s",
                    idx, len(query.keywords), query.base_name, query.keywords)


async def _make_client(config, logger):
    auth = EbayAuth(config.token_file, config.ebay_client_id, config.ebay_client_secret, logger)
    token = await asyncio.to_thread(auth.get_access_token)
    return EbayClient(token, logger, config.api_concurrency,
                      buying_options=config.ebay_buying_options, auth=auth)


async def run_app(config_path: Path, script_dir: Path) -> int:
    try:
        config = load_config(config_path, script_dir)
        queries = build_queries(config)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    logger = setup_logging(script_dir / "fastebaysearch.log", config.log_to_console, config.log_max_size_mb)
    _log_queries(logger, queries)
    database = Database(config.db_path, logger)
    history_id = None
    start_time = time.time()
    exit_code = 1
    try:
        database.ensure_schema()
        history_run = database.new_history_run()
        history_id = history_run.id
        logger.info("History run started | run_number=%s | db=%s", history_run.run_number, config.db_path)
        database.set_history_timestamp(history_id, "start_time")
        client = await _make_client(config, logger)
        result = await Workflow(config, database, client, queries, logger).run()
        exit_code = result.exit_code
        log_summary(logger, result, start_time)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        logger.error("Run failed: %s", type(exc).__name__)
        exit_code = exit_code or 1
    finally:
        if history_id is not None:
            try:
                database.set_history_timestamp(history_id, "end_time")
            except (OSError, sqlite3.Error) as exc:
                logger.error("History update failed: %s", type(exc).__name__)
                exit_code = exit_code or 1
        logger.info("=============================================================================")
    return exit_code


async def run_clean_search(config_path: Path, script_dir: Path) -> int:
    try:
        config = load_config(config_path, script_dir)
        queries = build_queries(config)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    logger = setup_logging(script_dir / "fastebaysearch.log", config.log_to_console, config.log_max_size_mb)
    _log_queries(logger, queries)
    start_time = time.time()
    exit_code = 1
    try:
        client = await _make_client(config, logger)
        rates = await client.get_exchange_rates()
        search_run = await client.run_queries(config.ebay_sites, queries, rates)
        exit_code = search_run.exit_code
        unique_items = dedupe_by_item_id(search_run.items)
        person_name = config.email.person_name if config.email else "User"
        report_summary = search_run.summary
        if len(unique_items) > config.html_report_max_per_run:
            report_summary += (f". REPORT TRUNCATED: found={len(unique_items)}, "
                               f"reported={config.html_report_max_per_run}; clean search has no delivery queue")
        sent = HtmlReportNotifier(config.html_report_dir, config.html_report_max_per_run, logger).send(
            unique_items, person_name, run_summary=report_summary,
        )
        errors = []
        if len(sent) < len(unique_items):
            errors.append(f"Clean search report incomplete: found={len(unique_items)}, "
                          f"reported={len(sent)}; no delivery queue")
        result = WorkflowResult(search_run.items, unique_items, unique_items, search_run, errors)
        exit_code = result.exit_code
        log_summary(logger, result, start_time)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("Clean search failed: %s", type(exc).__name__)
        exit_code = exit_code or 1
    finally:
        logger.info("=============================================================================")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    script_dir = Path(__file__).resolve().parent.parent
    if args.estimate_api_budget:
        return run_api_budget_estimate(Path(args.config), script_dir)
    if args.clean_search:
        return asyncio.run(run_clean_search(Path(args.config), script_dir))
    return asyncio.run(run_app(Path(args.config), script_dir))
