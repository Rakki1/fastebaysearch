import sqlite3

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
