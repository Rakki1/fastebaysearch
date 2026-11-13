import asyncio
import json
from pathlib import Path

import pytest

from fastebaysearch_app.config import ConfigError, load_config
from fastebaysearch_app.ebay_client import EbayClient, SEARCH_URL
from fastebaysearch_app.models import SearchQuery
from test_config import minimal_config
from test_search_reliability import Response, Session


@pytest.mark.parametrize('value,expected', [({}, {}), ({'EBAY_GB':[]},{'EBAY_GB':[]}),
    ({'EBAY_GB':[131090,'131090','0015032']},{'EBAY_GB':['131090','15032']})])
def test_categories_normalized(tmp_path,value,expected):
    raw=minimal_config()
    raw['ebay_exclude_category_ids']=value
    path=tmp_path/'config.json'
    path.write_text(json.dumps(raw))
    assert load_config(path,tmp_path).ebay_exclude_category_ids == expected


@pytest.mark.parametrize('value', [None,[], '131090', {'EBAY_GB':'131090'}, {'GB':['131090']},
    {'EBAY_GB':[True]}, {'EBAY_GB':[0]}, {'EBAY_GB':[-1]}, {'EBAY_GB':[1.5]},
    {'EBAY_GB':['']}, {'EBAY_GB':['abc']}, {'EBAY_GB':['1|2']}, {'EBAY_GB':[None]}])
def test_invalid_categories_rejected(tmp_path,value):
    raw=minimal_config();raw['ebay_exclude_category_ids']=value
    path=tmp_path/'config.json';path.write_text(json.dumps(raw))
    with pytest.raises(ConfigError,match='ebay_exclude_category_ids'):
        load_config(path,tmp_path)


def test_missing_categories_default_to_empty(tmp_path):
    path=tmp_path/'config.json';path.write_text(json.dumps(minimal_config()))
    config=load_config(path,tmp_path)
    assert config.ebay_exclude_category_ids == {}
    assert config.ebay_search_in_description is True


@pytest.mark.parametrize('site', ['EBAY_GB','EBAY_US','EBAY_DE','EBAY_AU','EBAY_FR','EBAY_IT'])
def test_site_filter_survives_pagination_split_and_retry(site,monkeypatch):
    async def sleep(_): pass
    monkeypatch.setattr(asyncio,'sleep',sleep)
    mapping={s:['6028' if s=='EBAY_US' else '131090'] for s in ['EBAY_GB','EBAY_US','EBAY_DE','EBAY_AU','EBAY_FR','EBAY_IT']}
    session=Session([Response(data={'total':10001,'itemSummaries':[]}),Response(503),
        Response(data={'total':1,'itemSummaries':[{'itemId':'10001'}],'next':SEARCH_URL+'?offset=200'}),
        Response(data={'total':1,'itemSummaries':[{'itemId':'10002'}]}),Response(data={'total':0})])
    client=EbayClient('fake',exclude_category_ids=mapping)
    result=asyncio.run(client._search_with_session(session,site,'Bongo'))
    assert result.status == 'success'
    assert len(session.calls)==5
    for call in session.calls:
        assert call['headers']['X-EBAY-C-MARKETPLACE-ID']==site
        assert 'excludeCategoryIds:{'+mapping[site][0]+'}' in call['params']['filter']
        assert 'searchInDescription:true' in call['params']['filter']
        assert call['params']['q']=='Bongo'


@pytest.mark.parametrize('mapping', [{},{'EBAY_GB':[]},{'EBAY_US':['6028']}])
def test_no_filter_for_missing_or_empty_site(mapping):
    session=Session([Response()])
    asyncio.run(EbayClient('fake',exclude_category_ids=mapping)._search_with_session(session,'EBAY_GB','Bongo'))
    assert 'excludeCategoryIds' not in session.calls[0]['params']['filter']


def test_multiple_category_ids_and_logging(monkeypatch,caplog):
    import aiohttp
    import logging
    session=Session([Response()])
    monkeypatch.setattr(aiohttp,'ClientSession',lambda:session)
    caplog.set_level(logging.INFO)
    client=EbayClient('fake',exclude_category_ids={'EBAY_GB':['131090','15032']})
    asyncio.run(client.run_queries(['EBAY_GB'],[SearchQuery('Bongo','Bongo')],{}))
    assert 'excludeCategoryIds:{131090|15032}' in session.calls[0]['params']['filter']
    assert "Excluded categories for EBAY_GB: ['131090', '15032']" in caplog.text


def test_cli_passes_categories_to_shared_client(monkeypatch,tmp_path):
    from fastebaysearch_app import cli
    raw=minimal_config();raw['ebay_exclude_category_ids']={'EBAY_US':['6028']}
    path=tmp_path/'config.json';path.write_text(json.dumps(raw))
    config=load_config(path,tmp_path)
    monkeypatch.setattr(cli.EbayAuth,'get_access_token',lambda self:'fake')
    client=asyncio.run(cli._make_client(config,None))
    assert client.exclude_category_ids=={'EBAY_US':['6028']}


def test_example_starts_with_html_only_and_video_game_categories():
    root=Path(__file__).resolve().parents[1]
    config=load_config(root/'ebaysearch.example.json',root)
    assert config.ebay_search_in_description is False
    assert config.ebay_exclude_category_ids == {}
    assert config.ebay_category_ids == {'EBAY_GB': ['139973']}
    assert config.use_html_report is True
    assert config.use_email is False
    assert config.use_telegram is False
