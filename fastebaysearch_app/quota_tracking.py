"""Persist quota cooldowns and lease one Telegram warning per outage/window."""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass

from .analytics import AnalyticsError, BrowseQuota, format_reset
from .run_budget import SAMPLE_LIMIT, SAMPLE_MAX_AGE


def migrate(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS api_quota_state (
        app_key TEXT PRIMARY KEY, episode_id TEXT NOT NULL, reset_at REAL,
        next_check_at REAL NOT NULL, message TEXT NOT NULL,
        telegram_notified_at REAL, claim_until REAL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS api_run_costs (
        id INTEGER PRIMARY KEY, app_key TEXT NOT NULL, profile TEXT NOT NULL,
        observed_at REAL NOT NULL, calls INTEGER NOT NULL)""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_api_run_costs ON api_run_costs(app_key, profile, id)")


@dataclass(frozen=True)
class QuotaBlock:
    episode_id: str
    reset_at: float | None
    next_check_at: float
    message: str
    telegram_notified_at: float | None = None
    claim_until: float | None = None


class QuotaStore:
    def __init__(self, connect, client_id):
        self.connect = connect
        self.key = hashlib.sha256(("production:" + client_id).encode()).hexdigest()

    def _read(self, conn):
        row = conn.execute("SELECT episode_id, reset_at, next_check_at, message, telegram_notified_at, claim_until "
                           "FROM api_quota_state WHERE app_key=?", (self.key,)).fetchone()
        return QuotaBlock(*row) if row else None

    def get(self):
        with self.connect() as conn:
            return self._read(conn)

    def block(self, quota: BrowseQuota | None, now: float, budget_summary: str = ""):
        reset = quota.reset_at(now) if quota and quota.exhausted else None
        details = quota.summary() if quota else "Browse API returned HTTP 429; quota details unavailable."
        if quota and not quota.exhausted:
            details = "Browse API returned HTTP 429. Analytics has not confirmed an exhausted quota. " + details
        message = "eBay Browse API limit reached.\n" + details
        if reset:
            message += "\nSearches resume at the next scheduled run after " + format_reset(reset) + "."
        else:
            message += "\nA reliable reset time is unavailable. The limit will be checked again on a later scheduled run."
        if budget_summary:
            message += "\n\n" + budget_summary
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = self._read(conn)
            new_window = bool(old and old.reset_at is not None and old.reset_at <= now and reset and reset != old.reset_at)
            if not old or new_window:
                state = QuotaBlock(uuid.uuid4().hex, reset, reset or now + 900, message)
            else:
                # Learning a previously unknown reset time must not send a second warning.
                state = QuotaBlock(old.episode_id, reset, reset or now + 900, message,
                                   old.telegram_notified_at, old.claim_until)
            conn.execute("INSERT OR REPLACE INTO api_quota_state VALUES (?,?,?,?,?,?,?)",
                         (self.key, state.episode_id, state.reset_at, state.next_check_at, state.message,
                          state.telegram_notified_at, state.claim_until))
            return state

    def record_run_cost(self, profile, calls, now):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO api_run_costs(app_key,profile,observed_at,calls) VALUES (?,?,?,?)",
                         (self.key, profile, now, calls))
            conn.execute("DELETE FROM api_run_costs WHERE app_key=? AND profile=? AND id NOT IN "
                         "(SELECT id FROM api_run_costs WHERE app_key=? AND profile=? ORDER BY id DESC LIMIT ?)",
                         (self.key, profile, self.key, profile, SAMPLE_LIMIT))

    def run_costs(self, profile, now):
        with self.connect() as conn:
            return [row[0] for row in conn.execute(
                "SELECT calls FROM api_run_costs WHERE app_key=? AND profile=? AND observed_at>=? "
                "ORDER BY id DESC LIMIT ?", (self.key, profile, now - SAMPLE_MAX_AGE, SAMPLE_LIMIT))]

    def clear(self, episode_id):
        with self.connect() as conn:
            conn.execute("DELETE FROM api_quota_state WHERE app_key=? AND episode_id=?", (self.key, episode_id))

    def claim_alert(self, episode_id, now):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._read(conn)
            if (not row or row.episode_id != episode_id or row.telegram_notified_at is not None
                    or (row.claim_until is not None and row.claim_until > now)):
                return False
            conn.execute("UPDATE api_quota_state SET claim_until=? WHERE app_key=? AND episode_id=?",
                         (now + 300, self.key, episode_id))
            return True

    def finish_alert(self, episode_id, sent, now):
        with self.connect() as conn:
            conn.execute("UPDATE api_quota_state SET telegram_notified_at=?, claim_until=NULL "
                         "WHERE app_key=? AND episode_id=?", (now if sent else None, self.key, episode_id))


class QuotaMonitor:
    def __init__(self, store, fetch, logger, clock=time.time):
        self.store, self.fetch, self.logger, self.clock = store, fetch, logger, clock
        self.active = None
        self.budget = None

    def _block(self, quota, now):
        summary = self.budget.summary(quota, now) if self.budget else ""
        return self.store.block(quota, now, summary)

    async def _fetch(self):
        try:
            quota = await self.fetch()
        except (AnalyticsError, OSError, RuntimeError, ValueError) as exc:
            self.logger.warning("Browse quota lookup unavailable: %s", str(exc) if isinstance(exc, AnalyticsError) else type(exc).__name__)
            return None
        self.logger.info(quota.summary())
        return quota

    async def before_search(self):
        self.active = self.store.get()
        now = self.clock()
        if self.active and now < self.active.next_check_at:
            self.logger.warning("Browse searches paused: %s", self.active.message)
            return self.active
        quota = await self._fetch()
        if quota and quota.exhausted:
            self.active = self._block(quota, now)
            return self.active
        return None

    async def after_search(self, run):
        if self.budget:
            self.budget.record(run, self.clock())
        if run.exit_code == 2:
            self.active = self._block(await self._fetch(), self.clock())
            run.quota_message = self.active.message
        elif self.active and (run.items or any(outcome.pages for outcome in run.outcomes)):
            self.store.clear(self.active.episode_id)
            self.active = None

    async def notify_once(self, telegram):
        state = self.active
        if not state or not self.store.claim_alert(state.episode_id, self.clock()):
            return True
        sent = False
        try:
            sent = bool(await telegram.send_quota_status(state.message))
            return sent
        finally:
            self.store.finish_alert(state.episode_id, sent, self.clock())
