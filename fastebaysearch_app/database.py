from __future__ import annotations

import logging
import sqlite3
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

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        return conn

    def ensure_schema(self) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.cursor()
            cur.executescript(
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
                    telegram_error TEXT
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
                """
            )
            cur.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_item_id ON ebayids (item_id);
                CREATE INDEX IF NOT EXISTS idx_seller ON ebayids (seller);
                CREATE INDEX IF NOT EXISTS idx_email_pending ON ebayids (email_notified_at);
                CREATE INDEX IF NOT EXISTS idx_telegram_pending ON ebayids (telegram_notified_at);
                CREATE INDEX IF NOT EXISTS idx_exchange_rates_updated_at ON exchange_rates (updated_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_history_run_number_unique ON history (run_number);
                """
            )
            self._seed_history(cur)
            conn.commit()

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

    def claim_new_results(self, results: list[SearchResult]) -> list[SearchResult]:
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
            conn.commit()

        self.logger.info(f"Added {len(inserted)} new items to the database.")
        return inserted

    def pending_email_notifications(self, limit: int | None = None) -> list[SearchResult]:
        return self._pending_channel_notifications("email_notified_at", limit)

    def pending_telegram_notifications(self, limit: int | None = None) -> list[SearchResult]:
        return self._pending_channel_notifications("telegram_notified_at", limit)

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

        return [
            SearchResult(
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
            for row in rows
        ]

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


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]
