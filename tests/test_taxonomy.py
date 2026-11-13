import json
from unittest.mock import Mock

import pytest
import requests

from fastebaysearch_app import cli
from fastebaysearch_app.config import ConfigError, load_category_lookup_config
from fastebaysearch_app.taxonomy import (
    CategorySuggestion, TaxonomyClient, TaxonomyError, TaxonomyRateLimit, parse_suggestions,
)


def suggestion(category_id='139973'):
    return {'category': {'categoryId': category_id, 'categoryName': 'Video Games'},
            'categoryTreeNodeAncestors': [
                {'categoryId': '99', 'categoryName': 'Subcategory', 'categoryTreeNodeLevel': 2},
                {'categoryId': '1249', 'categoryName': 'Video Games & Consoles', 'categoryTreeNodeLevel': 1}]}


def response(status=200, data=None):
    result = Mock(status_code=status)
    result.json.return_value = {} if data is None else data
    return result


def client_session(responses, monkeypatch):
    monkeypatch.setattr('fastebaysearch_app.taxonomy.time.sleep', lambda seconds: None)
    auth = Mock()
    auth.get_access_token.side_effect = ['first-token', 'refreshed-token']
    client = TaxonomyClient(auth)
    session = Mock()
    session.get.side_effect = responses
    return client, session, auth


def test_paths_sort_ancestors_and_dedupe_suggestions():
    parsed = parse_suggestions({'categorySuggestions': [suggestion(), suggestion()]})
    assert parsed == [CategorySuggestion('139973',
        'Video Games & Consoles [1249] > Subcategory [99] > Video Games [139973]')]


@pytest.mark.parametrize('data', [{}, {'categorySuggestions': None}, {'categorySuggestions': [{}]},
    {'categorySuggestions': [suggestion('../bad')]},
    {'categorySuggestions': [{'category': {'categoryId': '0', 'categoryName': 'Invalid'}}]}])
def test_invalid_suggestions_rejected(data):
    with pytest.raises(TaxonomyError):
        parse_suggestions(data)


def test_marketplace_tree_and_unicode_query_are_sent_as_parameters(monkeypatch):
    client, session, _ = client_session([
        response(data={'categoryTreeId': '0'}),
        response(data={'categorySuggestions': [suggestion()]})], monkeypatch)
    assert client.find_categories(session, 'EBAY_US', 'Pokémon & cards')[0].category_id == '139973'
    first, second = session.get.call_args_list
    assert first.kwargs['params'] == {'marketplace_id': 'EBAY_US'}
    assert second.args[0].endswith('/category_tree/0/get_category_suggestions')
    assert second.kwargs['params'] == {'q': 'Pokémon & cards'}
    assert first.kwargs['timeout'] == 20


def test_refresh_only_once_across_marketplaces(monkeypatch):
    responses = [response(401), response(data={'categoryTreeId': '3'}),
                 response(data={'categorySuggestions': []}), response(401)]
    client, session, auth = client_session(responses, monkeypatch)
    assert client.find_categories(session, 'EBAY_GB', 'c64') == []
    with pytest.raises(TaxonomyError, match='HTTP 401'):
        client.find_categories(session, 'EBAY_US', 'c64')
    assert auth.get_access_token.call_count == 2
    assert session.get.call_args.kwargs['headers']['Authorization'] == 'Bearer refreshed-token'
    for result in responses:
        result.close.assert_called_once()


def test_transient_retry_and_no_content(monkeypatch):
    client, session, _ = client_session([response(503), requests.Timeout('sensitive'),
        response(data={'categoryTreeId': '3'}), response(204)], monkeypatch)
    assert client.find_categories(session, 'EBAY_GB', 'c64') == []
    assert session.get.call_count == 4


def test_retry_exhaustion_hides_transport_details(monkeypatch):
    client, session, _ = client_session([requests.ConnectionError('secret')] * 3, monkeypatch)
    with pytest.raises(TaxonomyError, match='Network error: ConnectionError') as exc:
        client.find_categories(session, 'EBAY_GB', 'c64')
    assert 'secret' not in str(exc.value)
    assert session.get.call_count == 3


@pytest.mark.parametrize('result,exception', [
    (response(429), TaxonomyRateLimit), (response(400), TaxonomyError),
    (response(data={'categoryTreeId': '../escape'}), TaxonomyError),
    (response(data={'errors': [{'message': 'secret'}]}), TaxonomyError)])
def test_api_failures_are_safe(result, exception, monkeypatch):
    client, session, _ = client_session([result], monkeypatch)
    with pytest.raises(exception) as exc:
        client.find_categories(session, 'EBAY_GB', 'c64')
    assert 'secret' not in str(exc.value)
    assert session.get.call_count == 1


def write_config(tmp_path, sites=None):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'ebay_client_id': 'fake-id', 'ebay_client_secret': 'fake-secret',
        'ebay_sites': sites or ['EBAY_GB', 'EBAY_US'], 'use_email': True, 'use_telegram': True,
        'ebay_search_keywords': None, 'ebay_category_ids': 'irrelevant'}))
    return path


def test_lookup_config_ignores_listing_channels_and_dedupes_sites(tmp_path):
    config = load_category_lookup_config(write_config(tmp_path, ['EBAY_US', 'EBAY_US']), tmp_path)
    assert config.ebay_sites == ['EBAY_US']
    assert config.token_file == tmp_path / 'oauth_token.json'


def test_token_path_cannot_escape(tmp_path):
    path = write_config(tmp_path)
    raw = json.loads(path.read_text())
    raw['ebay_oauth_file'] = '../outside.json'
    path.write_text(json.dumps(raw))
    with pytest.raises(ConfigError):
        load_category_lookup_config(path, tmp_path)


@pytest.mark.parametrize('flag', ['--clean-search', '--estimate-api-budget'])
def test_lookup_is_exclusive(flag):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(['config.json', '--find-categories', 'commodore 64', flag])


def test_lookup_routes_from_main(monkeypatch):
    runner = Mock(return_value=0)
    monkeypatch.setattr(cli, 'run_find_categories', runner)
    assert cli.main(['example.json', '--find-categories', 'commodore 64']) == 0
    assert runner.call_args.args[2] == 'commodore 64'


def test_empty_query_fails_before_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'EbayAuth', lambda *a: pytest.fail('No auth for invalid input'))
    assert cli.run_find_categories(write_config(tmp_path), tmp_path, '  ') == 1


@pytest.mark.parametrize('error,exit_code', [(None, 0), (TaxonomyError('HTTP 400'), 1),
                                          (TaxonomyRateLimit('HTTP 429'), 2)])
def test_cli_outputs_results_without_notifications_database_or_config_changes(tmp_path, monkeypatch, capsys, error, exit_code):
    path = write_config(tmp_path, ['EBAY_GB', 'EBAY_US', 'EBAY_DE'])
    original = path.read_bytes()
    monkeypatch.setattr(cli, 'EbayAuth', Mock())
    fake = Mock()
    fake.find_categories.side_effect = [
        [CategorySuggestion('139973', 'Video Games & Consoles [1249] > Video Games [139973]')],
        error if error else [], []]
    monkeypatch.setattr(cli, 'TaxonomyClient', lambda auth: fake)
    for name in ('Database', 'Workflow', 'EbayClient', 'HtmlReportNotifier', 'setup_logging'):
        monkeypatch.setattr(cli, name, lambda *a, **k: pytest.fail('Unexpected side effect'))
    assert cli.run_find_categories(path, tmp_path, 'commodore 64') == exit_code
    output = capsys.readouterr()
    assert 'Video Games [139973]' in output.out
    assert '"EBAY_GB": [' in output.out
    assert '"EBAY_US": [' not in output.out  # Empty/failed sites must not silently disable filters.
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ['config.json']
    assert fake.find_categories.call_count == (2 if exit_code == 2 else 3)
    assert ('HTTP' in output.err) == bool(error)
