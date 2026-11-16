import asyncio

import aiohttp
import pytest

from fastebaysearch_app.ebay_client import EbayClient, SEARCH_URL
from fastebaysearch_app.models import SearchQuery
from http_fakes import Response, Session


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
