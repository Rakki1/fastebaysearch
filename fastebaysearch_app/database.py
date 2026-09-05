from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import SearchResult
from .utils import utc_now_str

SQLITE_VARIABLE_LIMIT = 900


@dataclass(frozen=True)
class HistoryRun:
    id: int
    run_number: int


class Database:
    def __init__(self, db_path: Path, logger: logging.Logger | None = None):
        self.db_path = Path(db_path)
        self.logger = logger or logging.getLogger("fastebaysearch")

    @contextmanager
    def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute("PRAGMA journal_mode=WAL;")
            with conn:
                yield conn
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.cursor()
            _execute_schema_statements(cur,
                """
                CREATE TABLE IF NOT EXISTS ebayids (
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
                    telegram_error TEXT,
                    telegram_attempted_at TEXT,
                    telegram_attempt_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY,
                    run_number INTEGER,
                    start_time TEXT,
                    end_time TEXT
                );
                CREATE TABLE IF NOT EXISTS exchange_rates (
                    currency TEXT PRIMARY KEY,
                    rate REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS html_notifications (
                    item_id TEXT PRIMARY KEY REFERENCES ebayids(item_id),
                    queued_at TEXT NOT NULL,
                    delivered_at TEXT,
                    report_path TEXT,
                    last_error TEXT
                );
                """
            )
            self._ensure_ebayids_columns(cur)
            _execute_schema_statements(cur,
                """
                CREATE INDEX IF NOT EXISTS idx_item_id ON ebayids (item_id);
                CREATE INDEX IF NOT EXISTS idx_seller ON ebayids (seller);
                CREATE INDEX IF NOT EXISTS idx_email_pending ON ebayids (email_notified_at);
                CREATE INDEX IF NOT EXISTS idx_telegram_pending ON ebayids (telegram_notified_at);
                CREATE INDEX IF NOT EXISTS idx_email_pending_id ON ebayids (id) WHERE email_notified_at IS NULL;
                CREATE INDEX IF NOT EXISTS idx_telegram_pending_id ON ebayids (id) WHERE telegram_notified_at IS NULL;
                CREATE INDEX IF NOT EXISTS idx_telegram_claim ON ebayids (
                    telegram_notified_at, telegram_attempted_at, telegram_attempt_count, id
                );
                CREATE INDEX IF NOT EXISTS idx_exchange_rates_updated_at ON exchange_rates (updated_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_history_run_number_unique ON history (run_number);
                CREATE INDEX IF NOT EXISTS idx_html_pending ON html_notifications (queued_at, item_id)
                    WHERE delivered_at IS NULL;
                """
            )
            self._seed_history(cur)
            conn.commit()

    def _ensure_ebayids_columns(self, cur: sqlite3.Cursor) -> None:
        columns = {str(row[1]) for row in cur.execute("PRAGMA table_info(ebayids);").fetchall()}
        if "telegram_attempted_at" not in columns:
            cur.execute("ALTER TABLE ebayids ADD COLUMN telegram_attempted_at TEXT;")
        if "telegram_attempt_count" not in columns:
            cur.execute("ALTER TABLE ebayids ADD COLUMN telegram_attempt_count INTEGER NOT NULL DEFAULT 0;")

    def _seed_history(self, cur: sqlite3.Cursor) -> None:
        cur.execute("SELECT COUNT(*) FROM history;")
        if int(cur.fetchone()[0]) == 0:
            cur.execute("INSERT INTO history (id, run_number, start_time, end_time) VALUES (0, 0, NULL, NULL);")

    def new_history_run(self) -> HistoryRun:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.cursor()
            cur.execute("SELECT MAX(run_number) FROM history;")
            max_run = int(cur.fetchone()[0] or 0)
            run_number = max_run + 1
            cur.execute(
                "INSERT INTO history (run_number, start_time, end_time) VALUES (?, NULL, NULL);",
                (run_number,),
            )
            conn.commit()
            return HistoryRun(id=int(cur.lastrowid), run_number=run_number)

    def set_history_timestamp(self, history_id: int | None, timestamp_type: str) -> None:
        if history_id is None or timestamp_type not in {"start_time", "end_time"}:
            return
        with self.connect() as conn:
            conn.execute(
                f"UPDATE history SET {timestamp_type} = ? WHERE id = ?;",
                (utc_now_str(), int(history_id)),
            )
            conn.commit()

    def claim_new_results(self, results: list[SearchResult], enqueue_html: bool = False) -> list[SearchResult]:
        if not results:
            return []

        insert_time = utc_now_str()
        unique_results: list[SearchResult] = []
        seen_item_ids: set[str] = set()
        for result in results:
            if result.item_id and result.item_id not in seen_item_ids:
                seen_item_ids.add(result.item_id)
                unique_results.append(result)

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.cursor()
            existing_item_ids: set[str] = set()
            for chunk in _chunks([result.item_id for result in unique_results], SQLITE_VARIABLE_LIMIT):
                placeholders = ",".join("?" for _ in chunk)
                rows = cur.execute(f"SELECT item_id FROM ebayids WHERE item_id IN ({placeholders});", chunk).fetchall()
                existing_item_ids.update(str(row[0]) for row in rows)

            inserted = [result for result in unique_results if result.item_id not in existing_item_ids]
            cur.executemany(
                """
                INSERT OR IGNORE INTO ebayids (
                    item_id, keywords, title, url, site, price, seller, starts, ends, image, insert_time
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        result.item_id,
                        result.keywords,
                        result.name,
                        result.link.split("?")[0],
                        result.ebay_site,
                        result.price,
                        result.seller,
                        result.starts,
                        result.ends,
                        result.image,
                        insert_time,
                    )
                    for result in inserted
                ],
            )
            if enqueue_html:
                cur.executemany(
                    "INSERT INTO html_notifications (item_id, queued_at) VALUES (?, ?);",
                    [(result.item_id, insert_time) for result in inserted],
                )
            conn.commit()

        self.logger.info(f"Added {len(inserted)} new items to the database.")
        return inserted

    def deliver_html_notifications(self, notifier, limit: int, person_name: str,
                                   run_summary: str = "") -> bool:
        # Hold the write lock only during local file I/O, never during network calls.
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            rows = conn.execute(
                """
                SELECT e.keywords, e.title, e.site, e.price, e.item_id, e.url,
                       e.seller, e.starts, e.ends, e.image
                FROM html_notifications h JOIN ebayids e ON e.item_id = h.item_id
                WHERE h.delivered_at IS NULL
                ORDER BY h.queued_at, e.id LIMIT ?;
                """, (max(1, int(limit)),),
            ).fetchall()
            pending = [_search_result_from_row(row) for row in rows]
            if not pending:
                return True
            try:
                sent = notifier.send(pending, person_name, run_summary=run_summary)
                sent_ids = {result.item_id for result in sent}
            except Exception as exc:
                self.logger.error("HTML report notification failed: %s", type(exc).__name__)
                sent_ids = set()
            path = getattr(notifier, "last_report_path", None)
            now = utc_now_str()
            conn.executemany(
                """UPDATE html_notifications
                   SET delivered_at = ?, report_path = ?, last_error = ? WHERE item_id = ?;""",
                [(now if result.item_id in sent_ids else None,
                  str(path) if path is not None and result.item_id in sent_ids else None,
                  None if result.item_id in sent_ids else "HTML report write failed",
                  result.item_id) for result in pending],
            )
            remaining = conn.execute(
                "SELECT COUNT(*) FROM html_notifications WHERE delivered_at IS NULL;"
            ).fetchone()[0]
            self.logger.info("HTML queue: delivered=%s pending=%s", len(sent_ids), remaining)
            return all(result.item_id in sent_ids for result in pending)

    def pending_email_notifications(self, limit: int | None = None) -> list[SearchResult]:
        return self._pending_channel_notifications("email_notified_at", limit)

    def pending_telegram_notifications(self, limit: int | None = None) -> list[SearchResult]:
        return self._pending_channel_notifications("telegram_notified_at", limit)

    def claim_pending_telegram_notifications(
        self,
        limit: int | None = None,
        retry_after_hours: int = 6,
        max_attempts: int = 3,
    ) -> list[SearchResult]:
        now = utc_now_str()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(0, int(retry_after_hours)))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        max_attempts = max(1, int(max_attempts))
        params: list[object] = [max_attempts, cutoff]
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(max(1, int(limit)))

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            rows = conn.execute(
                f"""
                SELECT keywords, title, site, price, item_id, url, seller, starts, ends, image
                FROM ebayids
                WHERE telegram_notified_at IS NULL
                  AND telegram_attempt_count < ?
                  AND (telegram_attempted_at IS NULL OR telegram_attempted_at <= ?)
                ORDER BY id ASC
                {limit_sql};
                """,
                tuple(params),
            ).fetchall()

            item_ids = [row[4] for row in rows]
            conn.executemany(
                """
                UPDATE ebayids
                SET telegram_attempted_at = ?,
                    telegram_attempt_count = telegram_attempt_count + 1,
                    telegram_error = NULL
                WHERE item_id = ?;
                """,
                [(now, item_id) for item_id in item_ids],
            )
            conn.commit()

        return [_search_result_from_row(row) for row in rows]

    def release_telegram_claims(self, results: list[SearchResult], error: str) -> None:
        if not results:
            return

        with self.connect() as conn:
            conn.executemany(
                """
                UPDATE ebayids
                SET telegram_attempted_at = NULL,
                    telegram_attempt_count = CASE
                        WHEN telegram_attempt_count > 0 THEN telegram_attempt_count - 1
                        ELSE 0
                    END,
                    telegram_error = ?
                WHERE item_id = ?
                  AND telegram_notified_at IS NULL;
                """,
                [(error, result.item_id) for result in results],
            )
            conn.commit()

    def _pending_channel_notifications(self, notified_column: str, limit: int | None = None) -> list[SearchResult]:
        if notified_column not in {"email_notified_at", "telegram_notified_at"}:
            raise ValueError(f"Invalid notification column: {notified_column}")
        params: tuple[int, ...] = ()
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            params = (max(1, int(limit)),)
        query = f"""
            SELECT keywords, title, site, price, item_id, url, seller, starts, ends, image
            FROM ebayids
            WHERE {notified_column} IS NULL
            ORDER BY id ASC
            {limit_sql};
        """
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [_search_result_from_row(row) for row in rows]

    def mark_email_succeeded(self, results: list[SearchResult]) -> None:
        self._mark_channel(results, "email_notified_at", "email_error", utc_now_str(), None)

    def mark_email_failed(self, results: list[SearchResult], error: str) -> None:
        self._mark_channel(results, "email_notified_at", "email_error", None, error)

    def mark_telegram_succeeded(self, results: list[SearchResult]) -> None:
        self._mark_channel(results, "telegram_notified_at", "telegram_error", utc_now_str(), None)

    def mark_telegram_failed(self, results: list[SearchResult], error: str) -> None:
        self._mark_channel(results, "telegram_notified_at", "telegram_error", None, error)

    def _mark_channel(
        self,
        results: list[SearchResult],
        notified_column: str,
        error_column: str,
        notified_at: str | None,
        error: str | None,
    ) -> None:
        if not results:
            return
        if notified_column not in {"email_notified_at", "telegram_notified_at"}:
            raise ValueError(f"Invalid notification column: {notified_column}")
        if error_column not in {"email_error", "telegram_error"}:
            raise ValueError(f"Invalid notification error column: {error_column}")

        item_ids = [result.item_id for result in results]
        with self.connect() as conn:
            conn.executemany(
                f"UPDATE ebayids SET {notified_column} = ?, {error_column} = ? WHERE item_id = ?;",
                [(notified_at, error, item_id) for item_id in item_ids],
            )
            conn.commit()

    def get_cached_exchange_rates(self, max_age_seconds: int | None = None) -> dict[str, float]:
        params: tuple[str, ...] = ()
        where_sql = ""
        if max_age_seconds is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(0, int(max_age_seconds)))
            where_sql = "WHERE updated_at >= ?"
            params = (cutoff.strftime("%Y-%m-%d %H:%M:%S"),)

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT currency, rate
                FROM exchange_rates
                {where_sql}
                ORDER BY currency;
                """,
                params,
            ).fetchall()

        return {str(currency): float(rate) for currency, rate in rows}

    def replace_exchange_rates(self, rates: dict[str, float]) -> None:
        cleaned_rates = {
            str(currency).upper(): float(rate)
            for currency, rate in rates.items()
            if currency and isinstance(rate, int | float) and float(rate) > 0
        }
        if not cleaned_rates:
            return

        updated_at = utc_now_str()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("DELETE FROM exchange_rates;")
            conn.executemany(
                "INSERT INTO exchange_rates (currency, rate, updated_at) VALUES (?, ?, ?);",
                [(currency, rate, updated_at) for currency, rate in cleaned_rates.items()],
            )
            conn.commit()


def _execute_schema_statements(cur: sqlite3.Cursor, sql: str) -> None:
    # executescript implicitly commits an existing transaction; keep migrations atomic.
    for statement in sql.split(";"):
        if statement.strip():
            cur.execute(statement)


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _search_result_from_row(row) -> SearchResult:
    return SearchResult(
        keywords=row[0] or "",
        name=row[1] or "",
        ebay_site=row[2] or "",
        price=row[3] or "",
        item_id=row[4] or "",
        link=row[5] or "",
        seller=row[6] or "",
        starts=row[7] or "",
        ends=row[8] or "",
        image=row[9] or "",
    )
