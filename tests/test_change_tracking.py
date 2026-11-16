import pytest

from fastebaysearch_app.database import Database
from listing_fixtures import item
from fastebaysearch_app.notifications.html_report import HtmlReportNotifier


@pytest.fixture
def db(tmp_path):
    value = Database(tmp_path/'test.db')
    value.ensure_schema()
    return value


def test_price_change_delivery_is_idempotent_and_channel_specific(db):
    original = item()
    db.claim_new_results([original],enqueue_html=True)
    db.mark_email_succeeded([original])
    reduced = item(fixed_amount='15',price='15 GBP')
    db.claim_new_results([reduced],enqueue_html=True)
    db.claim_new_results([reduced],enqueue_html=True)
    pending = db.pending_email_notifications(10)
    assert len(pending) == 1 and pending[0].event_id
    assert '20 -> 15 GBP' in pending[0].change_description
    telegram = db.claim_pending_telegram_notifications(10)
    assert len(telegram) == 1 and not telegram[0].event_id
    assert telegram[0].price == '15 GBP'
    db.mark_email_failed(pending,'temporary')
    assert db.pending_email_notifications(10) == pending
    db.mark_email_succeeded(pending)
    assert db.pending_email_notifications(10) == []


@pytest.mark.parametrize('changes', [dict(fixed_amount='25'),dict(fixed_amount=''),
    dict(fixed_amount='15',fixed_currency='USD'),dict(fixed_amount='15',source_site='EBAY_US'),
    dict(bid_amount='30'),dict(name='Changed title')])
def test_non_notifiable_changes_only_update_snapshot(db,changes):
    db.claim_new_results([item()])
    db.mark_email_succeeded([item()])
    db.claim_new_results([item(**changes)])
    assert db.pending_email_notifications() == []


def test_buying_options_change_and_telegram_retry(db):
    db.claim_new_results([item()])
    db.mark_telegram_succeeded([item()])
    db.claim_new_results([item(buying_options=('AUCTION',),fixed_amount='',bid_amount='10')])
    pending = db.claim_pending_telegram_notifications(1)
    assert pending[0].event_id and 'Buying options' in pending[0].change_description
    assert db.claim_pending_telegram_notifications(1) == []
    db.release_telegram_claims(pending,'header failed')
    assert db.claim_pending_telegram_notifications(1) == pending
    db.mark_telegram_succeeded(pending)
    assert db.claim_pending_telegram_notifications(1) == []


def test_legacy_baseline_does_not_replay(db):
    db.claim_new_results([item()])
    db.mark_email_succeeded([item()])
    with db.connect() as conn:
        conn.execute('DELETE FROM item_observations')
    db.ensure_schema()
    db.claim_new_results([item(fixed_amount='10')])
    assert db.pending_email_notifications() == []


def test_html_changes_and_status_report(db,tmp_path):
    notifier = HtmlReportNotifier(tmp_path,10)
    db.claim_new_results([item()],enqueue_html=True)
    assert db.deliver_html_notifications(notifier,10,'User')
    db.claim_new_results([item(fixed_amount='10')],enqueue_html=True)
    assert db.deliver_html_notifications(notifier,10,'User')
    assert 'Price decreased' in notifier.last_report_path.read_text(encoding='utf8')
    assert db.deliver_html_notifications(notifier,10,'User','PARTIAL SEARCH: failed=1')
    assert 'PARTIAL SEARCH' in notifier.last_report_path.read_text(encoding='utf8')


def test_new_and_changed_listings_share_limit(db):
    db.claim_new_results([item()])
    db.mark_email_succeeded([item()])
    db.claim_new_results([item(fixed_amount='10')])
    new = item(item_id='10002')
    db.claim_new_results([new])
    first = db.pending_email_notifications(1)
    assert len(first) == 1 and first[0].event_id
    db.mark_email_succeeded(first)
    second = db.pending_email_notifications(1)
    assert len(second) == 1
    assert first[0].notification_key != second[0].notification_key
    db.mark_email_succeeded(second)
    assert db.pending_email_notifications(1) == []


def test_pending_telegram_inspection_does_not_claim_change(db):
    db.claim_new_results([item()])
    db.mark_telegram_succeeded([item()])
    db.claim_new_results([item(fixed_amount='10')])
    first = db.pending_telegram_notifications(1)
    assert first == db.pending_telegram_notifications(1)
    assert db.claim_pending_telegram_notifications(1) == first
    assert db.claim_pending_telegram_notifications(1) == []
