import asyncio
import json
import sqlite3

import pytest

from fastebaysearch_app import cli
from fastebaysearch_app.analytics import BrowseQuota, QuotaRate
from fastebaysearch_app.config import load_config
from fastebaysearch_app.database import Database
from fastebaysearch_app.models import SearchRun
from fastebaysearch_app.query_builder import build_queries
from fastebaysearch_app.quota_tracking import QuotaStore
from fastebaysearch_app.run_budget import RuntimeBudget, SAMPLE_MAX_AGE
from test_cli import FakeEbayAuth, minimal_config, write_config
from test_run_budget import cached_run


NOW = 1800000000.0


@pytest.fixture
def quota_command(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Quota reports must not write history, run searches or send notifications")

    def run(path, script_dir, remaining=3840):
        async def lookup(self):
            return BrowseQuota((QuotaRate(5000, 5000 - remaining, remaining, NOW + 86400, 86400),))

        monkeypatch.setattr(cli, "EbayAuth", FakeEbayAuth)
        monkeypatch.setattr(cli.EbayClient, "get_browse_quota", lookup)
        monkeypatch.setattr(cli.EbayClient, "run_queries", forbidden)
        monkeypatch.setattr(cli.EbayClient, "get_exchange_rates", forbidden)
        monkeypatch.setattr(cli, "Workflow", forbidden)
        monkeypatch.setattr(cli, "setup_logging", forbidden)
        monkeypatch.setattr(Database, "connect", forbidden)
        monkeypatch.setattr(Database, "ensure_schema", forbidden)
        monkeypatch.setattr(cli.time, "time", lambda: NOW)
        return asyncio.run(cli.run_api_quota(path, script_dir))

    return run


def test_quota_command_reports_current_cached_costs_from_wal_without_writing(tmp_path, capsys, quota_command):
    data = minimal_config()
    data["ebay_sites"] = ["EBAY_GB", "EBAY_US", "EBAY_DE", "EBAY_AU", "EBAY_FR", "EBAY_IT"]
    data["ebay_search_keywords"]["base_terms"] = [f"term{i}" for i in range(13)]
    data["ebay_daily_api_limit"] = 100000  # Recommendations must use the actual Analytics limit.
    data["ebay_urls_dbfile"] = "history #1.db"
    path = write_config(tmp_path, data)
    cfg = load_config(path, tmp_path)
    database = Database(cfg.db_path)
    database.ensure_schema()
    store = QuotaStore(database.connect, cfg.ebay_client_id)
    budget = RuntimeBudget(store, cfg, build_queries(cfg))

    with database.connect() as anchor:
        # Keep WAL open so the report must read committed samples beyond the main database file.
        anchor.execute("SELECT COUNT(*) FROM api_run_costs").fetchone()
        budget.record(cached_run(), NOW - 1800)
        budget.record(cached_run(description_calls=0), NOW - 1)
        wal = cfg.db_path.with_name(cfg.db_path.name + "-wal")
        assert wal.stat().st_size > 0
        before = cfg.db_path.read_bytes(), wal.read_bytes()

        data.update(use_email=True, use_telegram=True, telegram_send_mode="unconfigured")
        path.write_text(json.dumps(data), encoding="utf-8")
        assert quota_command(path, tmp_path) == 0
        assert (cfg.db_path.read_bytes(), wal.read_bytes()) == before

    output = capsys.readouterr().out
    assert "limit=5000, used=1160, remaining=3840" in output
    assert "13 queries, 6 marketplaces, at least 78 search calls/run" in output
    assert "541 Browse calls" in output
    assert "Warm-cache base: 78 calls/run" in output
    assert "463 description calls per 24h" in output
    assert "Recommended: 49 complete runs/24h, interval at least 29 minutes" in output
    assert "90% quota budget" in output
    assert "cap 500 is below" in output
    assert "unavailable" not in output
    assert not (tmp_path / "fastebaysearch.log").exists()


@pytest.mark.parametrize("history", ["empty", "different_profile", "expired", "other_application"])
def test_quota_command_does_not_recommend_frequency_without_matching_recent_history(
    tmp_path, capsys, quota_command, history
):
    data = minimal_config()
    path = write_config(tmp_path, data)
    cfg = load_config(path, tmp_path)
    database = Database(cfg.db_path)
    database.ensure_schema()
    client_id = "another-application" if history == "other_application" else cfg.ebay_client_id
    budget = RuntimeBudget(QuotaStore(database.connect, client_id), cfg, build_queries(cfg))
    if history != "empty":
        observed = NOW - SAMPLE_MAX_AGE - 1 if history == "expired" else NOW - 1
        budget.record(SearchRun(browse_calls=541), observed)
    if history == "different_profile":
        data["ebay_search_keywords"]["base_terms"] = ["another title"]
        path.write_text(json.dumps(data), encoding="utf-8")
    before = cfg.db_path.read_bytes()

    assert quota_command(path, tmp_path) == 0

    output = capsys.readouterr().out
    assert "remaining=3840" in output
    assert "has not been measured for this configuration" in output
    assert "Recommended:" not in output
    assert "541 Browse calls" not in output
    assert cfg.db_path.read_bytes() == before


@pytest.mark.parametrize("has_cost_table", [False, True])
def test_quota_command_reads_legacy_history_without_migrating(tmp_path, capsys, quota_command, has_cost_table):
    path = write_config(tmp_path)
    cfg = load_config(path, tmp_path)
    database = Database(cfg.db_path)
    store = QuotaStore(database.connect_readonly, cfg.ebay_client_id)
    budget = RuntimeBudget(store, cfg, build_queries(cfg))
    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute("CREATE TABLE history (id INTEGER PRIMARY KEY)")
        if has_cost_table:
            conn.execute("CREATE TABLE api_run_costs (id INTEGER PRIMARY KEY, app_key TEXT, "
                         "profile TEXT, observed_at REAL, calls INTEGER)")
            conn.execute("INSERT INTO api_run_costs VALUES (1, ?, ?, ?, 541)",
                         (store.key, budget.profile, NOW - 1))
    before = cfg.db_path.read_bytes()

    assert quota_command(path, tmp_path) == 0

    output = capsys.readouterr().out
    assert "remaining=3840" in output
    if has_cost_table:
        assert "541 Browse calls" in output
        assert "Recommended: 8 complete runs/24h, interval at least 180 minutes" in output
    else:
        assert "has not been measured" in output
        assert "Recommended:" not in output
    assert cfg.db_path.read_bytes() == before


@pytest.mark.parametrize("remaining,expected", [(3840, 0), (0, 2)])
def test_unreadable_history_does_not_hide_quota_or_change_exit_status(
    tmp_path, capsys, quota_command, remaining, expected
):
    path = write_config(tmp_path)
    database_path = tmp_path / "items.db"
    before = b"not a SQLite database"
    database_path.write_bytes(before)

    assert quota_command(path, tmp_path, remaining) == expected

    output = capsys.readouterr().out
    assert f"remaining={remaining}" in output
    assert "Measured search budget unavailable: DatabaseError" in output
    assert "Recommended:" not in output
    assert database_path.read_bytes() == before


def test_credentials_only_configuration_still_returns_quota(tmp_path, capsys, quota_command):
    data = {key: value for key, value in minimal_config().items()
            if key in {"ebay_client_id", "ebay_client_secret", "ebay_sites"}}
    path = write_config(tmp_path, data)

    assert quota_command(path, tmp_path) == 0

    output = capsys.readouterr().out
    assert "remaining=3840" in output
    assert "Measured search budget unavailable:" in output
    assert "Recommended:" not in output
    assert not (tmp_path / "items.db").exists()
