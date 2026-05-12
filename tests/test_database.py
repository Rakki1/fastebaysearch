import sqlite3
from datetime import datetime, timedelta, timezone

from fastebaysearch_app.database import Database
from fastebaysearch_app.models import SearchResult


def result(item_id="12345"):
    return SearchResult(
        keywords="camera",
        name="Camera",
        ebay_site="EBAY_US",
        price="10.00 EUR",
        item_id=item_id,
        link="https://example.com/item?x=1",
        seller="seller",
        starts="Not available",
        ends="Not available",
    )


def test_claim_new_results_returns_only_inserted_rows(tmp_path):
    db = Database(tmp_path / "items.db")
    db.ensure_schema()

    first = db.claim_new_results([result()])
    second = db.claim_new_results([result()])

    assert first == [result()]
    assert second == []


def test_claim_new_results_deduplicates_batch(tmp_path):
    db = Database(tmp_path / "items.db")
    db.ensure_schema()

    inserted = db.claim_new_results([result("1"), result("1"), result("2")])

    assert [item.item_id for item in inserted] == ["1", "2"]
    with sqlite3.connect(tmp_path / "items.db") as conn:
        count = conn.execute("SELECT COUNT(*) FROM ebayids;").fetchone()[0]
    assert count == 2


def test_pending_channel_notifications_respect_limit(tmp_path):
    db = Database(tmp_path / "items.db")
    db.ensure_schema()
    db.claim_new_results([result("1"), result("2")])

    assert [item.item_id for item in db.pending_email_notifications(limit=1)] == ["1"]
    assert [item.item_id for item in db.pending_telegram_notifications(limit=1)] == ["1"]


def test_ensure_schema_adds_telegram_attempt_columns_to_existing_database(tmp_path):
    db_path = tmp_path / "items.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE ebayids (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT UNIQUE NOT NULL,
                keywords TEXT,
                title TEXT,
                url TEXT,
                site TEXT,
                price TEXT,
                seller TEXT,
                starts TEXT,
                ends TEXT,
                image TEXT,
                insert_time TEXT NOT NULL,
                email_notified_at TEXT,
                telegram_notified_at TEXT,
                email_error TEXT,
                telegram_error TEXT
            );
            """
        )

    Database(db_path).ensure_schema()

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ebayids);").fetchall()}
    assert "telegram_attempted_at" in columns
    assert "telegram_attempt_count" in columns


def test_claim_pending_telegram_notifications_marks_attempt_and_respects_cooldown(tmp_path):
    db = Database(tmp_path / "items.db")
    db.ensure_schema()
    db.claim_new_results([result("1"), result("2")])

    claimed = db.claim_pending_telegram_notifications(limit=1, retry_after_hours=6, max_attempts=3)
    second_claim = db.claim_pending_telegram_notifications(limit=10, retry_after_hours=6, max_attempts=3)

    assert [item.item_id for item in claimed] == ["1"]
    assert [item.item_id for item in second_claim] == ["2"]
    with sqlite3.connect(tmp_path / "items.db") as conn:
        row = conn.execute(
            "SELECT telegram_attempted_at, telegram_attempt_count, telegram_error FROM ebayids WHERE item_id = '1';"
        ).fetchone()
    assert row[0] is not None
    assert row[1] == 1
    assert row[2] is None


def test_claim_pending_telegram_notifications_retries_after_cooldown(tmp_path):
    db = Database(tmp_path / "items.db")
    db.ensure_schema()
    db.claim_new_results([result("1")])
    db.claim_pending_telegram_notifications(limit=1, retry_after_hours=6, max_attempts=3)
    old_attempt = (datetime.now(timezone.utc) - timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(tmp_path / "items.db") as conn:
        conn.execute(
            "UPDATE ebayids SET telegram_attempted_at = ?, telegram_error = ? WHERE item_id = '1';",
            (old_attempt, "Telegram notification failed"),
        )

    claimed = db.claim_pending_telegram_notifications(limit=1, retry_after_hours=6, max_attempts=3)

    assert [item.item_id for item in claimed] == ["1"]
    with sqlite3.connect(tmp_path / "items.db") as conn:
        count = conn.execute("SELECT telegram_attempt_count FROM ebayids WHERE item_id = '1';").fetchone()[0]
    assert count == 2


def test_claim_pending_telegram_notifications_stops_after_max_attempts(tmp_path):
    db = Database(tmp_path / "items.db")
    db.ensure_schema()
    db.claim_new_results([result("1")])
    old_attempt = (datetime.now(timezone.utc) - timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(tmp_path / "items.db") as conn:
        conn.execute(
            """
            UPDATE ebayids
            SET telegram_attempted_at = ?, telegram_attempt_count = 3, telegram_error = ?
            WHERE item_id = '1';
            """,
            (old_attempt, "Telegram notification failed"),
        )

    claimed = db.claim_pending_telegram_notifications(limit=1, retry_after_hours=6, max_attempts=3)

    assert claimed == []


def test_claim_pending_telegram_notifications_ignores_succeeded_items(tmp_path):
    db = Database(tmp_path / "items.db")
    db.ensure_schema()
    db.claim_new_results([result("1")])
    db.mark_telegram_succeeded([result("1")])

    claimed = db.claim_pending_telegram_notifications(limit=1, retry_after_hours=6, max_attempts=3)

    assert claimed == []


def test_release_telegram_claims_reverses_header_only_attempt(tmp_path):
    db = Database(tmp_path / "items.db")
    db.ensure_schema()
    item = result("1")
    db.claim_new_results([item])
    claimed = db.claim_pending_telegram_notifications(limit=1, retry_after_hours=6, max_attempts=3)

    db.release_telegram_claims(claimed, "Telegram header notification failed")

    with sqlite3.connect(tmp_path / "items.db") as conn:
        row = conn.execute(
            """
            SELECT telegram_attempted_at, telegram_attempt_count, telegram_error
            FROM ebayids
            WHERE item_id = '1';
            """
        ).fetchone()
    assert row == (None, 0, "Telegram header notification failed")
    assert [retry.item_id for retry in db.claim_pending_telegram_notifications(limit=1)] == ["1"]


def test_exchange_rates_are_cached_in_database(tmp_path):
    db = Database(tmp_path / "items.db")
    db.ensure_schema()

    db.replace_exchange_rates({"USD": 1.2, "GBP": 0.9})

    assert db.get_cached_exchange_rates(max_age_seconds=3600) == {"GBP": 0.9, "USD": 1.2}


def test_history_timestamps_update_single_history_row(tmp_path):
    db = Database(tmp_path / "items.db")
    db.ensure_schema()

    first = db.new_history_run()
    second = db.new_history_run()
    db.set_history_timestamp(first.id, "start_time")
    db.set_history_timestamp(second.id, "end_time")

    with sqlite3.connect(tmp_path / "items.db") as conn:
        rows = conn.execute(
            "SELECT id, start_time, end_time FROM history WHERE id IN (?, ?) ORDER BY id;",
            (first.id, second.id),
        ).fetchall()

    assert rows[0][1] is not None
    assert rows[0][2] is None
    assert rows[1][1] is None
    assert rows[1][2] is not None
