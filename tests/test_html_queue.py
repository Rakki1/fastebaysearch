import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from fastebaysearch_app.config import load_config
from fastebaysearch_app.database import Database
from fastebaysearch_app.models import SearchResult, SearchRun
from fastebaysearch_app.notifications.html_report import HtmlReportNotifier
from fastebaysearch_app.workflow import Workflow


def item(number):
    return SearchResult("camera", f"Camera {number}", "EBAY_GB", "10 GBP", str(number),
                        f"https://www.ebay.co.uk/itm/{number}", "seller", "", "")


def queue_rows(db):
    with db.connect() as conn:
        return conn.execute("SELECT item_id, delivered_at, report_path, last_error FROM html_notifications ORDER BY item_id").fetchall()


def test_migration_from_061_preserves_history_and_does_not_enqueue_old_items(tmp_path):
    db = Database(tmp_path / "items.db")
    # Schema and data from 0.6.1, before HTML delivery tracking existed.
    with db.connect() as conn:
        conn.executescript("""
            CREATE TABLE ebayids (
                id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT UNIQUE NOT NULL,
                keywords TEXT, title TEXT, url TEXT, site TEXT, price TEXT, seller TEXT,
                starts TEXT, ends TEXT, image TEXT, insert_time TEXT NOT NULL,
                email_notified_at TEXT, telegram_notified_at TEXT, email_error TEXT,
                telegram_error TEXT, telegram_attempted_at TEXT,
                telegram_attempt_count INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE history (id INTEGER PRIMARY KEY, run_number INTEGER, start_time TEXT, end_time TEXT);
            CREATE TABLE exchange_rates (currency TEXT PRIMARY KEY, rate REAL NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO ebayids (item_id, insert_time, email_notified_at, telegram_attempt_count)
                VALUES ('11111', '2026-01-01', '2026-01-02', 2);
            INSERT INTO history VALUES (1, 10, '2026-01-01', '2026-01-02');
            INSERT INTO exchange_rates VALUES ('USD', 1.2, '2026-01-01');
        """)
        before = {table: conn.execute(f"SELECT * FROM {table}").fetchall()
                  for table in ("ebayids", "history", "exchange_rates")}
    db.ensure_schema()
    db.ensure_schema()
    with db.connect() as conn:
        after = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in before}
    assert before == after
    assert queue_rows(db) == []
    assert db.claim_new_results([item(11111)], enqueue_html=True) == []
    assert queue_rows(db) == []
    db.claim_new_results([item(22222)], enqueue_html=True)
    assert [row[0] for row in queue_rows(db)] == ["22222"]


def test_schema_failure_rolls_back_entire_migration(tmp_path, monkeypatch):
    db = Database(tmp_path / "items.db")

    def fail(cur):
        raise RuntimeError("migration interrupted")

    monkeypatch.setattr(db, "_seed_history", fail)
    with pytest.raises(RuntimeError):
        db.ensure_schema()
    with db.connect() as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_item_and_queue_insert_are_atomic(tmp_path):
    db = Database(tmp_path / "items.db")
    db.ensure_schema()
    with db.connect() as conn:
        conn.execute("""CREATE TRIGGER reject_html BEFORE INSERT ON html_notifications
                        BEGIN SELECT RAISE(ABORT, 'simulated queue failure'); END""")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.claim_new_results([item(11111)], enqueue_html=True)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ebayids").fetchone()[0] == 0


def test_html_limit_drains_across_runs_and_channel_toggle(tmp_path):
    cfg = load_config(Path(__file__).parent / "fixtures" / "collector_search.json", tmp_path)
    cfg = replace(cfg, use_html_report=True, html_report_dir=tmp_path / "reports",
                  html_report_max_per_run=1, exchange_rate_cache_ttl_hours=0)
    db = Database(cfg.db_path)
    db.ensure_schema()

    class Client:
        results = [item(11111), item(22222)]

        async def get_exchange_rates(self):
            return {}

        async def run_queries(self, *args):
            return SearchRun(items=self.results)

    client = Client()
    first = asyncio.run(Workflow(cfg, db, client, []).run())
    assert len(first.new_results) == 2
    assert first.exit_code == 0  # Pending items beyond the normal limit are safely queued.
    assert [bool(row[1]) for row in queue_rows(db)] == [True, False]
    client.results = [item(33333)]
    asyncio.run(Workflow(replace(cfg, use_html_report=False), db, client, []).run())
    assert len(queue_rows(db)) == 2
    assert [bool(row[1]) for row in queue_rows(db)] == [True, False]
    client.results = []
    second = asyncio.run(Workflow(cfg, db, client, []).run())
    assert second.new_results == []
    assert all(row[1] and Path(row[2]).is_file() for row in queue_rows(db))
    assert len(list(cfg.html_report_dir.glob("*.html"))) == 2


def test_failed_atomic_write_is_retried_next_run(tmp_path, monkeypatch):
    import fastebaysearch_app.notifications.html_report as report_module
    db = Database(tmp_path / "items.db")
    db.ensure_schema()
    db.claim_new_results([item(11111)], enqueue_html=True)
    report = HtmlReportNotifier(tmp_path / "reports")

    def fail(*args):
        raise OSError("simulated disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(report_module.os, "replace", fail)
        assert db.deliver_html_notifications(report, 1000, "Tester") is False
    row = queue_rows(db)[0]
    assert row[1] is None and row[2] is None and row[3]
    assert list(report.report_dir.iterdir()) == []
    assert db.deliver_html_notifications(report, 1000, "Tester", "PARTIAL SEARCH: failed=1") is True
    row = queue_rows(db)[0]
    assert row[1] and row[2] and row[3] is None
    assert "PARTIAL SEARCH" in Path(row[2]).read_text(encoding="utf-8")


def test_two_concurrent_deliveries_do_not_report_same_batch(tmp_path):
    db = Database(tmp_path / "items.db")
    db.ensure_schema()
    db.claim_new_results([item(11111), item(22222)], enqueue_html=True)
    barrier = Barrier(2)

    def deliver():
        barrier.wait(timeout=5)
        return Database(db.db_path).deliver_html_notifications(HtmlReportNotifier(tmp_path / "reports"), 1, "Tester")

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: deliver(), range(2))) == [True, True]
    rows = queue_rows(db)
    assert all(row[1] for row in rows)
    assert len({row[2] for row in rows}) == 2
    assert all(Path(row[2]).is_file() for row in rows)


def test_database_connections_close_after_context(tmp_path):
    import sqlite3
    db = Database(tmp_path / "items.db")
    db.ensure_schema()
    with db.connect() as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
