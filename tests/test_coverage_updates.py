import asyncio
from dataclasses import replace

import aiohttp
import pytest

from fastebaysearch_app.ebay_client import EbayClient, SEARCH_URL
from fastebaysearch_app.database import Database
from fastebaysearch_app.models import SearchResult, SearchQuery, SearchRun, QueryOutcome
from fastebaysearch_app.workflow import Workflow, dedupe_by_item_id
from fastebaysearch_app.notifications.html_report import HtmlReportNotifier
from test_search_reliability import Session, Response
from test_workflow import make_config, FakeEbayClient


def fetch(pages, **kwargs):
    session = Session([Response(data=p) for p in pages])
    client = EbayClient('fake', **kwargs)
    result = asyncio.run(client._search_with_session(session, 'EBAY_GB', 'camera', limit=2))
    return result, session, client


@pytest.mark.parametrize('items', [[], [{'itemId':'10001'}], [{'itemId':'10001'},{'itemId':'10002'}]])
def test_next_controls_pagination_even_if_total_reached(items):
    result, session, _ = fetch([
        {'total':len(items), 'itemSummaries':items, 'next':SEARCH_URL+'?offset=2'},
        {'total':1, 'itemSummaries':[{'itemId':'10003'}]}])
    assert result.status == 'success'
    assert len(session.calls) == 2
    assert result.raw_items[-1]['itemId'] == '10003'


@pytest.mark.parametrize('link', ['http://api.ebay.com/buy/browse/v1/item_summary/search?offset=2',
    SEARCH_URL+'?offset=0', SEARCH_URL+'?offset=1', SEARCH_URL+'?offset=no',
    'https://evil.example/search?offset=2', SEARCH_URL+'?offset=10000'])
def test_invalid_next_preserves_items(link):
    result, session, _ = fetch([{'total':1,'itemSummaries':[{'itemId':'10001'}],'next':link}])
    assert result.status == 'failed'
    assert len(result.raw_items) == 1
    assert len(session.calls) == 1


def test_split_keeps_boundaries_and_deduplicates():
    result, session, _ = fetch([
        {'total':10001,'itemSummaries':[{'itemId':'10001'}]},
        {'total':2,'itemSummaries':[{'itemId':'10001'},{'itemId':'10002'}]},
        {'total':1,'itemSummaries':[{'itemId':'10003'}]}])
    assert result.status == 'success'
    assert len(result.raw_items) == 3
    filters = [c['params']['filter'] for c in session.calls]
    assert len(set(filters)) == 3
    bounds = [s.split('itemStartDate:[')[1].split(']')[0].split('..') for s in filters]
    assert bounds[1][0] == bounds[2][1]
    assert bounds[0][0] == bounds[2][0]
    assert bounds[0][1] == bounds[1][1]


def test_budget_counts_retries_and_preserves_results(monkeypatch):
    async def sleep(_): pass
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    session = Session([Response(500), Response(data={'total':10001,'itemSummaries':[{'itemId':'10001'}]})])
    client = EbayClient('fake', max_search_calls=2)
    result = asyncio.run(client._search_with_session(session,'EBAY_GB','camera'))
    assert result.status == 'truncated'
    assert client.search_calls == len(session.calls) == 2
    assert len(result.raw_items) == 1


def test_budget_is_shared_across_searches(monkeypatch):
    session = Session(lambda _: Response(data={'total':10001,'itemSummaries':[]}))
    monkeypatch.setattr(aiohttp,'ClientSession',lambda:session)
    client = EbayClient('fake',max_search_calls=4,concurrency_limit=2)
    run = asyncio.run(client.run_queries(['EBAY_GB'],[SearchQuery(q,q) for q in ['one','two']],{}))
    assert client.search_calls == len(session.calls) == 4
    assert all(o.status == 'truncated' for o in run.outcomes)
    assert {c['params']['q'] for c in session.calls} == {'one','two'}


def test_description_can_be_disabled():
    _, session, _ = fetch([{'total':0}],search_in_description=False)
    assert 'searchInDescription' not in session.calls[0]['params']['filter']


def item(**kwargs):
    value = SearchResult('camera','Camera','EBAY_GB','20 GBP','10001','https://www.ebay.co.uk/itm/10001','seller','','',
                         buying_options=('FIXED_PRICE',), fixed_amount='20',fixed_currency='GBP',source_site='EBAY_GB')
    return replace(value, **kwargs)


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


@pytest.mark.parametrize('status', ['success','failed','truncated','rate_limited','cancelled'])
def test_empty_run_status_messages(db,status):
    calls = []
    class Client(FakeEbayClient):
        async def run_queries(self,*args):
            return SearchRun(outcomes=[QueryOutcome('EBAY_GB',SearchQuery('camera','camera'),status=status)])
    class Telegram:
        async def send_status(self,summary):
            calls.append(('telegram',summary))
            return True
    class Email:
        def send_status(self,summary):
            calls.append(('email',summary))
            return True
    result = asyncio.run(Workflow(make_config(),db,Client(),[],telegram_notifier=Telegram(),email_notifier=Email()).run())
    assert len(calls) == (0 if status == 'success' else 2)
    assert result.notification_errors == []


def test_source_marketplace_preference():
    foreign = item(source_site='EBAY_US')
    domestic = item()
    assert dedupe_by_item_id([foreign,domestic]) == [domestic]
    assert dedupe_by_item_id([domestic,foreign]) == [domestic]


@pytest.mark.parametrize('total', [9999,10000,10001])
def test_result_cap_partitions_instead_of_losing_old_items(total):
    root_filter = []
    def respond(kwargs):
        params = kwargs['params']
        if not root_filter:
            root_filter.append(params['filter'])
        if params['filter'] != root_filter[0]:
            return Response(data={'total':1,'itemSummaries':[{'itemId':'999999'}]})
        offset = int(params['offset'])
        items = [{'itemId':str(i+10000)} for i in range(offset,min(offset+200,total))]
        data = {'total':total,'itemSummaries':items}
        if offset+200 < total:
            data['next'] = SEARCH_URL+f'?offset={offset+200}'
        return Response(data=data)
    session = Session(respond)
    client = EbayClient('fake')
    result = asyncio.run(client._search_with_session(session,'EBAY_GB','camera'))
    assert result.status == 'success'
    if total == 9999:
        assert len(result.raw_items) == 9999
        assert len(session.calls) == 50
    else:
        assert any(i['itemId']=='999999' for i in result.raw_items)
        assert len({c['params']['filter'] for c in session.calls}) == 3


def test_unsplittable_interval_reports_partial():
    client = EbayClient('fake')
    client.run_end = 788918400001
    session = Session([Response(data={'total':10001,'itemSummaries':[]})])
    result = asyncio.run(client._search_with_session(session,'EBAY_GB','camera'))
    assert result.status == 'truncated'
    assert 'Unsplittable' in result.error


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



def test_email_status_uses_smtp_and_returns_boolean(monkeypatch):
    from fastebaysearch_app.notifications.email import EmailNotifier
    sent = []
    class SMTP:
        def __init__(self,*args,**kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def starttls(self,**kwargs): pass
        def login(self,*args): pass
        def send_message(self,message): sent.append(message)
    monkeypatch.setattr('smtplib.SMTP',SMTP)
    assert EmailNotifier(make_config().email).send_status('PARTIAL SEARCH: failed=1') is True
    assert len(sent) == 1
    assert 'PARTIAL SEARCH' in sent[0]['Subject']


def test_telegram_status_without_listings(monkeypatch):
    from test_telegram import Session as TelegramSession, Response as TelegramResponse
    from fastebaysearch_app.notifications.telegram import TelegramNotifier
    session = TelegramSession([TelegramResponse({'ok':True})])
    monkeypatch.setattr(aiohttp,'ClientSession',lambda **kwargs:session)
    assert asyncio.run(TelegramNotifier(make_config().telegram).send_status('PARTIAL SEARCH: failed=1')) is True
    assert 'failed=1' in session.calls[0][1]['json']['text']


def test_pending_telegram_inspection_does_not_claim_change(db):
    db.claim_new_results([item()])
    db.mark_telegram_succeeded([item()])
    db.claim_new_results([item(fixed_amount='10')])
    first = db.pending_telegram_notifications(1)
    assert first == db.pending_telegram_notifications(1)
    assert db.claim_pending_telegram_notifications(1) == first
    assert db.claim_pending_telegram_notifications(1) == []
