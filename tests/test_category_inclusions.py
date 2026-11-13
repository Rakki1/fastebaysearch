import asyncio
import json
import logging

import aiohttp
import pytest

from fastebaysearch_app import cli
from fastebaysearch_app.api_budget import estimate_api_budget
from fastebaysearch_app.config import ConfigError, load_config
from fastebaysearch_app.ebay_client import EbayClient, SEARCH_URL
from fastebaysearch_app.models import QueryOutcome, SearchQuery
from fastebaysearch_app.workflow import dedupe_by_item_id
from test_config import minimal_config
from test_search_reliability import Response, Session


@pytest.mark.parametrize('value', [None, [], '139973', {'GB': ['139973']},
    {'EBAY_GB': '139973'}, {'EBAY_GB': [True]}, {'EBAY_GB': [0]},
    {'EBAY_GB': [-1]}, {'EBAY_GB': [1.5]}, {'EBAY_GB': ['1,2']}])
def test_invalid_included_categories(tmp_path, value):
    raw = minimal_config()
    raw['ebay_category_ids'] = value
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(raw))
    with pytest.raises(ConfigError, match='ebay_category_ids'):
        load_config(path, tmp_path)


def test_normalization_and_shared_cli_wiring(tmp_path, monkeypatch):
    raw = minimal_config()
    raw['ebay_category_ids'] = {'EBAY_GB': [139973, '139973', '004193']}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(raw))
    config = load_config(path, tmp_path)
    monkeypatch.setattr(cli.EbayAuth, 'get_access_token', lambda self: 'fake')
    client = asyncio.run(cli._make_client(config, None))
    assert client.category_ids == {'EBAY_GB': ['139973', '4193']}
    raw.pop('ebay_category_ids')
    path.write_text(json.dumps(raw))
    assert load_config(path, tmp_path).ebay_category_ids == {}


@pytest.mark.parametrize('mapping', [{}, {'EBAY_GB': []}, {'EBAY_US': ['139973']}])
def test_unconfigured_site_searches_all_categories(monkeypatch, mapping):
    session = Session([Response()])
    monkeypatch.setattr(aiohttp, 'ClientSession', lambda: session)
    run = asyncio.run(EbayClient('fake', category_ids=mapping).run_queries(
        ['EBAY_GB'], [SearchQuery('Bongo', 'Bongo')], {}))
    assert len(run.outcomes) == 1
    assert run.exit_code == 0
    assert 'category_ids' not in session.calls[0]['params']


def test_multiple_categories_and_marketplaces_merge_for_delivery(monkeypatch, caplog):
    def respond(call):
        return Response(data={'total': 1, 'itemSummaries': [{
            'itemId': 'v1|123|0', 'title': 'C64 Bongo',
            'itemWebUrl': 'https://www.ebay.co.uk/itm/123',
            'price': {'value': '10', 'currency': 'EUR'}}]})
    session = Session(respond)
    monkeypatch.setattr(aiohttp, 'ClientSession', lambda: session)
    caplog.set_level(logging.INFO)
    client = EbayClient('fake', category_ids={'EBAY_GB': ['139973', '4193'], 'EBAY_US': ['139973']})
    run = asyncio.run(client.run_queries(['EBAY_GB', 'EBAY_US'], [SearchQuery('Bongo', 'Bongo')], {}))
    assert run.exit_code == 0
    assert [(o.site, o.category_id) for o in run.outcomes] == [
        ('EBAY_GB', '139973'), ('EBAY_GB', '4193'), ('EBAY_US', '139973')]
    assert len(session.calls) == 3
    assert len(run.items) == 3
    assert len(dedupe_by_item_id(run.items)) == 1
    assert 'category=4193' in caplog.text


def test_category_preserved_in_splits_pagination_and_retries(monkeypatch):
    async def sleep(_): pass
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    session = Session([Response(data={'total': 10001}), Response(503),
        Response(data={'total': 1, 'itemSummaries': [{'itemId': 'a'}],
                       'next': SEARCH_URL + '?offset=200&category_ids=999'}),
        Response(data={'total': 1, 'itemSummaries': [{'itemId': 'b'}]}), Response()])
    client = EbayClient('fake', exclude_category_ids={'EBAY_GB': ['131090']})
    outcome = QueryOutcome('EBAY_GB', SearchQuery('Bongo', 'Bongo'), category_id='139973')
    result = asyncio.run(client._search_with_session(session, 'EBAY_GB', 'Bongo', outcome=outcome))
    assert result.status == 'success'
    assert len(session.calls) == 5
    for call in session.calls:
        assert call['params']['category_ids'] == '139973'
        assert 'excludeCategoryIds:{131090}' in call['params']['filter']
        assert 'searchInDescription:true' in call['params']['filter']


@pytest.mark.parametrize('failure', ['warning', 'budget', 'rate_limit'])
def test_category_failures_are_visible_and_never_broaden_search(monkeypatch, failure):
    def respond(call):
        if call['params']['category_ids'] == '139973':
            return Response(data={'total': 1, 'itemSummaries': [{'itemId': 'valid'}]})
        if failure == 'rate_limit':
            return Response(429)
        return Response(data={'total': 100, 'warnings': [{'errorId': 12009}],
                              'itemSummaries': [{'itemId': 'unrestricted'}]})
    session = Session(respond)
    monkeypatch.setattr(aiohttp, 'ClientSession', lambda: session)
    client = EbayClient('fake', category_ids={'EBAY_GB': ['139973', '4193']},
                        max_search_calls=1 if failure == 'budget' else 500)
    run = asyncio.run(client.run_queries(['EBAY_GB'], [SearchQuery('Bongo', 'Bongo')], {}))
    assert run.outcomes[0].status == 'success'
    assert run.outcomes[1].status == {'warning': 'failed', 'budget': 'truncated', 'rate_limit': 'rate_limited'}[failure]
    assert run.exit_code == (2 if failure == 'rate_limit' else 1)
    assert run.outcomes[1].raw_items == []
    assert 'category=4193' in run.summary
    assert all(c['params'].get('category_ids') in ['139973', '4193'] for c in session.calls)


def test_budget_counts_categories_per_site():
    estimate = estimate_api_budget(['EBAY_GB', 'EBAY_US', 'EBAY_DE'],
        [SearchQuery('Bongo', 'Bongo')] * 13, estimated_results_per_query=400,
        category_ids={'EBAY_GB': ['139973', '4193'], 'EBAY_US': ['139973'], 'EBAY_DE': []})
    assert estimate.calls_per_run_minimum == 52
    assert estimate.calls_per_run_estimated == 104
    assert estimate.calls_per_run_maximum == 500
