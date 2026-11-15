import asyncio
import logging
from dataclasses import replace

import pytest

from fastebaysearch_app import workflow
from fastebaysearch_app.analytics import AnalyticsError, BrowseQuota, QuotaRate
from fastebaysearch_app.config import load_config, TelegramConfig
from fastebaysearch_app.database import Database
from fastebaysearch_app.models import QueryOutcome, SearchQuery, SearchRun
from fastebaysearch_app.quota_tracking import QuotaMonitor, QuotaStore
from test_cli import minimal_config, write_config
from test_database import result


NOW = 1800000000.0


def quota(remaining=0, reset=NOW + 3600):
    return BrowseQuota((QuotaRate(5000, 5000 - remaining, remaining, reset, 86400),))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "items.db")
    database.ensure_schema()
    return database


class Telegram:
    def __init__(self, sent=True):
        self.messages = []
        self.headers = []
        self.sent = sent

    async def send_quota_status(self, message):
        self.messages.append(message)
        await asyncio.sleep(0)
        return self.sent

    async def send_status(self, summary):
        pytest.fail("A quota episode must not also send repeated generic Telegram status messages")

    async def send_header(self, count, run_summary=""):
        self.headers.append((count, run_summary))
        return True

    async def send(self, pending):
        return pending


def test_quota_warning_survives_process_restart_and_uses_cached_cooldown(db):
    store = QuotaStore(db.connect, "app")
    calls = []

    async def fetch():
        calls.append(True)
        return quota()

    telegram = Telegram()
    for _ in range(3):
        monitor = QuotaMonitor(QuotaStore(db.connect, "app"), fetch, logging.getLogger(), clock=lambda: NOW)
        assert asyncio.run(monitor.before_search())
        assert asyncio.run(monitor.notify_once(telegram))
    assert len(calls) == 1
    assert len(telegram.messages) == 1
    assert "next scheduled run" in telegram.messages[0]
    assert store.get().telegram_notified_at == NOW
    assert QuotaStore(db.connect, "different-app").get() is None


def test_failed_telegram_delivery_is_retried_and_not_marked_notified(db):
    store = QuotaStore(db.connect, "app")
    state = store.block(quota(), NOW)
    monitor = QuotaMonitor(store, None, logging.getLogger(), clock=lambda: NOW)
    monitor.active = state
    telegram = Telegram(False)
    assert not asyncio.run(monitor.notify_once(telegram))
    assert store.get().telegram_notified_at is None
    assert store.get().claim_until is None
    telegram.sent = True
    assert asyncio.run(monitor.notify_once(telegram))
    assert store.get().telegram_notified_at == NOW


def test_simultaneous_runs_claim_only_one_warning(db):
    store = QuotaStore(db.connect, "app")
    state = store.block(quota(), NOW)
    monitors = [QuotaMonitor(QuotaStore(db.connect, "app"), None, logging.getLogger(), clock=lambda: NOW) for _ in range(2)]
    for monitor in monitors:
        monitor.active = state
    telegram = Telegram()

    async def send():
        await asyncio.gather(*(m.notify_once(telegram) for m in monitors))

    asyncio.run(send())
    assert len(telegram.messages) == 1


def test_new_quota_window_allows_new_warning_but_learning_reset_does_not(db):
    store = QuotaStore(db.connect, "app")
    old = store.block(None, NOW)
    assert store.claim_alert(old.episode_id, NOW)
    store.finish_alert(old.episode_id, True, NOW)
    known = store.block(quota(), NOW + 1)
    assert known.episode_id == old.episode_id
    assert known.telegram_notified_at == NOW
    new = store.block(quota(reset=NOW + 90000), NOW + 4000)
    assert new.episode_id != old.episode_id
    assert new.telegram_notified_at is None


def test_abandoned_claim_expires_and_old_worker_cannot_clear_new_window(db):
    store = QuotaStore(db.connect, "app")
    old = store.block(quota(), NOW)
    assert store.claim_alert(old.episode_id, NOW)
    assert not store.claim_alert(old.episode_id, NOW + 100)
    assert store.claim_alert(old.episode_id, NOW + 301)
    new = store.block(quota(reset=NOW + 90000), NOW + 4000)
    store.clear(old.episode_id)
    assert store.get().episode_id == new.episode_id


def test_unknown_reset_does_not_create_new_episode_on_repeated_429(db):
    store = QuotaStore(db.connect, "app")
    old = store.block(quota(remaining=100), NOW)  # Analytics may lag a short-term 429.
    assert old.reset_at is None
    assert old.next_check_at == NOW + 900
    assert "reliable reset time is unavailable" in old.message
    assert store.block(quota(remaining=100), NOW + 1800).episode_id == old.episode_id


class Client:
    def __init__(self, quotas, runs=()):
        self.quotas = iter(quotas)
        self.runs = iter(runs)
        self.quota_calls = 0
        self.search_calls = 0
        self.exchange_calls = 0

    async def get_browse_quota(self):
        self.quota_calls += 1
        value = next(self.quotas)
        if isinstance(value, Exception):
            raise value
        return value

    async def get_exchange_rates(self):
        self.exchange_calls += 1
        return {}

    async def run_queries(self, *args):
        self.search_calls += 1
        return next(self.runs)


def completed(status="success", items=()):
    return SearchRun(items=list(items), outcomes=[QueryOutcome("EBAY_GB", SearchQuery("camera", "camera"),
                                                              status=status, pages=1 if status == "success" else 0)])


def config(tmp_path):
    data = minimal_config()
    data["ebay_check_api_quota"] = True
    loaded = load_config(write_config(tmp_path, data), tmp_path)
    return replace(loaded, use_telegram=True, telegram=TelegramConfig("fake", "chat"))


def set_clock(monkeypatch, now):
    monkeypatch.setattr(workflow, "QuotaMonitor", lambda store, fetch, logger:
                        QuotaMonitor(store, fetch, logger, clock=lambda: now[0]))


def test_workflow_pauses_browse_and_resumes_after_reset_with_one_warning(tmp_path, db, monkeypatch):
    now = [NOW]
    set_clock(monkeypatch, now)
    cfg = config(tmp_path)
    telegram = Telegram()
    client = Client([quota(reset=NOW + 1200), quota(remaining=4999)], [completed()])

    def run():
        return asyncio.run(workflow.Workflow(cfg, db, client, [], telegram_notifier=telegram).run())

    assert run().exit_code == 2
    assert run().exit_code == 2
    assert client.search_calls == 0
    assert client.exchange_calls == 0
    assert client.quota_calls == 1
    assert len(telegram.messages) == 1
    now[0] = NOW + 1201
    assert run().exit_code == 0
    assert client.search_calls == 1
    assert client.quota_calls == 2
    assert QuotaStore(db.connect, cfg.ebay_client_id).get() is None


def test_paused_workflow_periodically_rechecks_quota_and_recovers_before_reported_reset(tmp_path, db, monkeypatch):
    now = [NOW]
    set_clock(monkeypatch, now)
    cfg = config(tmp_path)
    telegram = Telegram()
    exhausted = quota(reset=NOW + 86400)
    client = Client([exhausted, exhausted, quota(remaining=5000, reset=NOW + 86400)], [completed()])

    def run():
        return asyncio.run(workflow.Workflow(cfg, db, client, [], telegram_notifier=telegram).run())

    for elapsed, checks in ((0, 1), (1800, 1), (3600, 2), (5400, 2)):
        now[0] = NOW + elapsed
        assert run().exit_code == 2
        assert client.quota_calls == checks
        assert client.search_calls == 0
        assert client.exchange_calls == 0
    assert len(telegram.messages) == 1
    assert "checked again" in telegram.messages[0]
    now[0] = NOW + 7200
    assert run().exit_code == 0
    assert client.quota_calls == 3
    assert client.search_calls == 1
    assert len(telegram.messages) == 1
    assert QuotaStore(db.connect, cfg.ebay_client_id).get() is None


@pytest.mark.parametrize("remaining", [0, 5000])
def test_legacy_day_long_pause_is_rechecked_on_first_run_without_duplicate_warning(tmp_path, db, monkeypatch, remaining):
    set_clock(monkeypatch, [NOW])
    cfg = config(tmp_path)
    store = QuotaStore(db.connect, cfg.ebay_client_id)
    reset = NOW + 86400
    state = store.block(quota(reset=reset), NOW - 3600)
    store.finish_alert(state.episode_id, True, NOW - 3600)
    with db.connect() as conn:
        # State written by an older release waited all the way until reset.
        conn.execute("UPDATE api_quota_state SET next_check_at=? WHERE app_key=?", (reset, store.key))
    telegram = Telegram()
    client = Client([quota(remaining=remaining, reset=reset)], [completed()])

    outcome = asyncio.run(workflow.Workflow(cfg, db, client, [], telegram_notifier=telegram).run())

    assert client.quota_calls == 1
    assert telegram.messages == []
    if remaining:
        assert outcome.exit_code == 0
        assert client.search_calls == 1
        assert store.get() is None
    else:
        assert outcome.exit_code == 2
        assert client.search_calls == 0
        refreshed = store.get()
        assert refreshed.episode_id == state.episode_id
        assert refreshed.telegram_notified_at == NOW - 3600
        assert refreshed.next_check_at == NOW + 3600
        assert refreshed.reset_at == reset


def test_browse_429_refreshes_quota_and_partial_results_still_get_delivered(tmp_path, db, monkeypatch):
    set_clock(monkeypatch, [NOW])
    cfg = config(tmp_path)
    telegram = Telegram()
    client = Client([quota(remaining=10), quota()], [completed("rate_limited", [result()])])
    outcome = asyncio.run(workflow.Workflow(cfg, db, client, [], telegram_notifier=telegram).run())
    assert outcome.exit_code == 2
    assert client.quota_calls == 2
    assert len(telegram.messages) == 1
    assert telegram.headers == [(1, "")]
    assert db.pending_telegram_notifications() == []


def test_analytics_failure_does_not_stop_working_searches(tmp_path, db, monkeypatch):
    set_clock(monkeypatch, [NOW])
    cfg = config(tmp_path)
    client = Client([AnalyticsError("Analytics HTTP 503")], [completed()])
    outcome = asyncio.run(workflow.Workflow(cfg, db, client, [], telegram_notifier=Telegram()).run())
    assert outcome.exit_code == 0
    assert client.search_calls == 1


def test_repeated_429_with_positive_analytics_does_not_repeat_warning(tmp_path, db, monkeypatch):
    now = [NOW]
    set_clock(monkeypatch, now)
    cfg = config(tmp_path)
    telegram = Telegram()
    client = Client([quota(remaining=10)] * 4, [completed("rate_limited"), completed("rate_limited")])
    for _ in range(2):
        assert asyncio.run(workflow.Workflow(cfg, db, client, [], telegram_notifier=telegram).run()).exit_code == 2
        now[0] += 1800
    assert len(telegram.messages) == 1


def test_disabled_quota_monitor_does_not_call_analytics(tmp_path, db):
    cfg = replace(config(tmp_path), ebay_check_api_quota=False)
    client = Client([], [completed()])
    assert asyncio.run(workflow.Workflow(cfg, db, client, [], telegram_notifier=Telegram()).run()).exit_code == 0
    assert client.quota_calls == 0
