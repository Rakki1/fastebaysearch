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
from .ebay_client import EbayAuth, EbayClient, RateLimitError
from .logging_setup import setup_logging
from .query_builder import build_queries
from .workflow import Workflow, log_summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fastebaysearch.py")
    parser.add_argument("config", help="Path to JSON config file")
    parser.add_argument(
        "--estimate-api-budget",
        action="store_true",
        help="Estimate how many configured eBay searches can run per day without making API calls",
    )
    return parser


def run_api_budget_estimate(config_path: Path, script_dir: Path) -> int:
    try:
        config = load_config(config_path, script_dir)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    queries = build_queries(config)
    estimate = estimate_api_budget(
        config.ebay_sites,
        queries,
        daily_api_limit=config.ebay_daily_api_limit,
        safety_percent=config.api_budget_safety_percent,
        estimated_results_per_query=config.estimated_results_per_query,
    )
    print(format_api_budget_estimate(estimate))
    return 0


async def run_app(config_path: Path, script_dir: Path) -> int:
    try:
        config = load_config(config_path, script_dir)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    logger = setup_logging(script_dir / "fastebaysearch.log", config.log_to_console)
    queries = build_queries(config)
    logger.info(f"Generated {len(queries)} optimized queries.")
    for idx, query in enumerate(queries, start=1):
        status = " [OK]" if len(query.keywords) <= 100 else " [TOO LONG]"
        logger.info(f"  {idx}. ({len(query.keywords)}/100 chars){status} Base: {query.base_name} -> {query.keywords}")

    database = Database(config.db_path, logger)
    try:
        database.ensure_schema()
    except (OSError, sqlite3.Error) as exc:
        logger.error(f"Database error: {exc}")
        return 1

    history_id: int | None = None
    start_time = time.time()
    try:
        history_run = database.new_history_run()
        history_id = history_run.id
        logger.info(f"History run started | run_number={history_run.run_number} | db={config.db_path}")
        database.set_history_timestamp(history_id, "start_time")

        token = EbayAuth(config.token_file, config.ebay_client_id, config.ebay_client_secret, logger).get_access_token()
        workflow = Workflow(config, database, EbayClient(token, logger, config.api_concurrency), queries, logger)
        result = await workflow.run()
        log_summary(logger, result, start_time)
    except RateLimitError as exc:
        logger.critical(f"Rate limit hit; exiting with code 2: {exc}")
        return 2
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        logger.exception(f"Run failed: {exc}")
        return 1
    finally:
        if history_id is not None:
            database.set_history_timestamp(history_id, "end_time")
        logger.info("=============================================================================")

    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    script_dir = Path(__file__).resolve().parent.parent
    if args.estimate_api_budget:
        return run_api_budget_estimate(Path(args.config), script_dir)
    return asyncio.run(run_app(Path(args.config), script_dir))
