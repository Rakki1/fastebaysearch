import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import aiohttp
import pytest

from fastebaysearch_app.ebay_client import EbayClient, ITEM_URL, SEARCH_URL
from fastebaysearch_app.models import SearchQuery
from fastebaysearch_app.recommendation_filter import check_description, matches, positive_groups
from fastebaysearch_app.workflow import dedupe_by_item_id
from test_search_reliability import Response, Session


HTML = (Path(__file__).parent / "fixtures/glider_recommendations.html").read_text(encoding="utf-8")
TITLE = "Glider Rider - Commodore C64 / C128 Game On Cassette"
REST_ID = "v1|206651330408|0"
GROUPS = positive_groups("Anirog", ["commodore", "c64", "c-64"])


def query(base="Anirog"):
    return SearchQuery(base, base, positive_groups(base, ["commodore", "c64", "c-64"]))


@pytest.mark.parametrize("base,required,text,expected", [
    ("Anirog", ["c64", "c-64"], "ANIROG C-64", True),
    ("Anirog", [], "Anirogue", False),
    ("Loco Alligata", [], "Loco from Alligata", True),
    ('"Loco Alligata"', [], "Loco from Alligata", False),
    ('"Loco Alligata"', [], "Loco\nAlligata", True),
    ('(Slapshot, "Slap Shot")', ["c64"], "Slap Shot C64", True),
    ('(Slapshot, "Slap Shot")', ["c64"], "Slapshot C64", True),
    ("Kong -hong", [], "Kong Hong", True),  # Negatives remain eBay's responsibility.
    ('Kong -"Hong Kong"', [], "Kong", True),
    ("Five-A-Side", [], "Five-A-Side", True),
    ("Anirog", [], "Ａｎｉｒｏｇ", True),
])
def test_supported_positive_terms(base, required, text, expected):
    assert matches(text, positive_groups(base, required)) is expected


@pytest.mark.parametrize("base", ['Anirog*', 'title:Anirog', '(Anirog,)', '(Anirog', '"Anirog',
                                  '(Anirog (c64))', 'Anirog +c64', 'Anirog OR c64'])
def test_unfamiliar_syntax_is_not_guessed(base):
    # OR is deliberately unsupported, rather than treating the operator as a word.
    assert positive_groups(base, []) is None


def test_glider_matches_only_recommendations():
    assert check_description(TITLE, HTML, "206651330408", GROUPS).status == "rejected"


@pytest.mark.parametrize("title,html", [
    (TITLE + " Anirog", HTML),
    (TITLE, HTML + "<p>A genuine Anirog release.</p>"),
    (TITLE, "<p>A genuine Anirog release.</p>" + HTML),
])
def test_real_matches_survive(title, html):
    assert check_description(title, html, "206651330408", GROUPS).status == "accepted"


@pytest.mark.parametrize("html", [
    '<p>Recommended products: Slap Shot (Anirog)</p>',
    HTML.replace('wi-recommendations"', 'other-recommendations"'),
    HTML.replace('</div>', '', 1),
    HTML.replace('www.ebay.co.uk', 'www.ebay.co.uk.example.net'),
    HTML.replace('111111111111', '206651330408'),
    HTML.replace('<div class="wi-recommendations-products">', '<p>Anirog original description</p><div>'),
    HTML.replace('Anirog', 'Another Publisher'),
    '<script>' + HTML + '</script>',
])
def test_ambiguous_descriptions_are_retained(html):
    assert check_description(TITLE, html, "206651330408", GROUPS).status == "uncertain"


def item(title=TITLE, rest_id=REST_ID):
    return {"itemId": rest_id, "title": title, "listingMarketplaceId": "EBAY_GB"}


def search_response(items=None):
    return Response(data={"total": 1, "itemSummaries": [item()] if items is None else items})


def detail_response(html=HTML, rest_id=REST_ID):
    return Response(data={"itemId": rest_id, "description": html})


def run(monkeypatch, responses, queries=None, **kwargs):
    session = Session(responses)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    client = EbayClient("token", filter_recommendations=True, **kwargs)
    result = asyncio.run(client.run_queries(["EBAY_GB"], queries or [query()], {}))
    return result, client, session


def test_filter_precedes_final_dedup_and_caches_descriptions(monkeypatch):
    result, client, session = run(monkeypatch, [search_response(), search_response(), search_response(), detail_response()],
                                  [query(), query(), query("Glider Rider")])
    assert [i.keywords for i in dedupe_by_item_id(result.items)] == ["Glider Rider"]
    assert result.recommendations.rejected == 2
    assert result.recommendations.accepted == 1
    assert client.description_calls == 1
    assert len(session.calls) == 4
    assert result.outcomes[0].raw_items == [item()]
    assert "rejected=2" in result.summary


@pytest.mark.parametrize("kwargs", [{"filter_recommendations": False}, {"search_in_description": False}])
def test_disabled_filter_does_not_fetch_descriptions(monkeypatch, kwargs):
    session = Session([search_response()])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    client = EbayClient("token", **kwargs)
    result = asyncio.run(client.run_queries(["EBAY_GB"], [query()], {}))
    assert len(result.items) == 1
    assert result.recommendations is None
    assert len(session.calls) == 1


@pytest.mark.parametrize("response", [Response(404), detail_response(""), detail_response(rest_id="other"),
                                      Response(data={"shortDescription": "Anirog"}), Response(data={"errors": [{}]})])
def test_failed_or_incomplete_detail_keeps_result(monkeypatch, response):
    result, _, _ = run(monkeypatch, [search_response(), response])
    assert len(result.items) == 1
    assert result.recommendations.uncertain == 1
    assert result.exit_code == 0


def test_budget_is_shared_with_descriptions(monkeypatch):
    result, client, session = run(monkeypatch, [search_response()], max_search_calls=1)
    assert client.search_calls == 1
    assert client.description_calls == 0
    assert len(session.calls) == 1
    assert len(result.items) == 1
    assert result.recommendations.uncertain == 1


def test_detail_rate_limit_stops_waiting_calls_and_keeps_matches(monkeypatch):
    result, client, session = run(monkeypatch, [search_response([item(), item(rest_id="v1|333333333333|0")]), Response(429)],
                                  concurrency_limit=1)
    assert len(session.calls) == 2
    assert client.description_calls == 1
    assert len(result.items) == 2
    assert result.recommendations.uncertain == 2
    assert result.exit_code == 2
    assert "HTTP 429" in result.summary


@pytest.mark.parametrize("failure", [Response(503), asyncio.TimeoutError(), aiohttp.ClientError()])
def test_detail_retries_count_toward_budget(monkeypatch, failure):
    async def no_wait(_):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    result, client, _ = run(monkeypatch, [search_response(), failure, failure, detail_response()])
    assert not result.items
    assert client.search_calls == 4
    assert client.description_calls == 3


def test_detail_401_refresh_reuses_request_policy(monkeypatch):
    auth = Mock()
    auth.get_access_token.return_value = "new-token"
    result, client, session = run(monkeypatch, [search_response(), Response(401), detail_response()], auth=auth)
    assert not result.items
    auth.get_access_token.assert_called_once_with(force_refresh=True)
    assert session.calls[-1]["headers"]["Authorization"] == "Bearer new-token"
    assert client.description_calls == 2


def test_cache_uses_rest_id_variation_and_search_marketplace(monkeypatch):
    from urllib.parse import unquote

    class RoutedSession(Session):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if url == SEARCH_URL:
                return search_response([item(), item(rest_id="v1|206651330408|42")])
            rest_id = unquote(url.removeprefix(ITEM_URL))
            return detail_response(rest_id=rest_id)

    session = RoutedSession([])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    client = EbayClient("token", filter_recommendations=True)
    result = asyncio.run(client.run_queries(["EBAY_GB", "EBAY_US"], [query(), query()], {}))
    details = [(url, kw) for url, kw in session.calls if url.startswith(ITEM_URL)]
    assert len(details) == 4
    assert {kw["headers"]["X-EBAY-C-MARKETPLACE-ID"] for _, kw in details} == {"EBAY_GB", "EBAY_US"}
    assert {url for url, _ in details} == {ITEM_URL + "v1%7C206651330408%7C0", ITEM_URL + "v1%7C206651330408%7C42"}
    assert result.recommendations.rejected == 8


def test_normal_workflow_filters_new_records_and_preserves_old_queue(tmp_path, monkeypatch):
    from urllib.parse import unquote
    from fastebaysearch_app.config import load_config
    from fastebaysearch_app.database import Database
    from fastebaysearch_app.models import SearchResult
    from fastebaysearch_app.workflow import Workflow
    from test_config import minimal_config

    ids = [REST_ID, "v1|333333333333|0", "v1|444444444444|0", "v1|555555555555|0"]

    class RoutedSession(Session):
        def get(self, url, **kwargs):
            if url == SEARCH_URL:
                return search_response([item(rest_id=ids[0]), item(rest_id=ids[1]),
                                        item(title="Anirog Commodore C64", rest_id=ids[2]), item(rest_id=ids[3])])
            rest_id = unquote(url.removeprefix(ITEM_URL))
            html = "<p>A cassette game.</p>" if rest_id == ids[3] else HTML
            return detail_response(html, rest_id)

    class Client(EbayClient):
        async def get_exchange_rates(self):
            return {}

    class Report:
        sent = []

        def send(self, items, person_name, run_summary=""):
            self.sent = list(items)
            return self.sent

    data = minimal_config()
    data.update(use_html_report=True, html_report_max_per_run=1000, ebay_filter_recommendations=True)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    config = load_config(path, tmp_path)
    database = Database(config.db_path)
    database.ensure_schema()
    old = SearchResult("Anirog", "Old queued listing", "EBAY_GB", "10 EUR", "206651330408", "", "seller", "", "")
    database.claim_new_results([old], enqueue_html=True)
    session = RoutedSession([])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    report = Report()
    result = asyncio.run(Workflow(config, database, Client("token", filter_recommendations=True), [query()],
                                  html_report_notifier=report).run())
    assert {r.item_id for r in result.new_results} == {"444444444444", "555555555555"}
    assert {r.item_id for r in report.sent} == {"206651330408", "444444444444", "555555555555"}
    with sqlite3.connect(config.db_path) as connection:
        rows = dict(connection.execute("SELECT item_id, title FROM ebayids"))
    assert "333333333333" not in rows
    assert rows["206651330408"] == "Old queued listing"
    assert result.exit_code == 0
