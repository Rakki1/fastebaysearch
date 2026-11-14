import asyncio
import itertools
import logging
from dataclasses import replace

import aiohttp
import pytest

from fastebaysearch_app import cli
from fastebaysearch_app.ebay_client import EbayClient
from fastebaysearch_app.models import SearchQuery
from fastebaysearch_app.query_builder import build_queries
from fastebaysearch_app.recommendation_filter import check_description, positive_groups
from test_cli import minimal_config, write_config
from test_recommendation_filter import HTML, REST_ID, TITLE, detail_response, item, search_response
from test_search_reliability import Response, Session


def query(base="Battle Through Time", required=("commodore", "c64", "c-64")):
    return SearchQuery(base, base, positive_groups(base, list(required)))


def check(title, body, base="Battle Through Time", required=("commodore", "c64", "c-64"), **kwargs):
    return check_description(title, body, "206651330408", query(base, required).positive_groups,
                             validate_terms=True, **kwargs)


PARTIAL_NAMES = [" ".join(words) for count in range(3)
                 for words in itertools.combinations(("battle", "through", "time"), count)]


@pytest.mark.parametrize("name", PARTIAL_NAMES)
def test_every_incomplete_battle_through_time_combination_is_rejected(name):
    decision = check(name + " C64", "<p>A cassette game.</p>")
    assert decision.status == "rejected"
    assert decision.reason == "missing_positive_terms"
    assert decision.missing_groups == tuple((word,) for word in ("battle", "through", "time")
                                             if word not in name.split())


@pytest.mark.parametrize("title,body", [
    ("C64 Time Through BATTLE", "<p>A cassette game.</p>"),
    ("Battle C64", "<p>Travel through time.</p>"),
    ("Battle Through", "<p>Time for the Commodore.</p>"),
    ("A cassette game", "<p>Battle</p><p>Through Time for C-64.</p>"),
])
def test_all_words_may_span_fields_and_use_any_order(title, body):
    assert check(title, body).status == "accepted"


def test_platform_alternative_is_mandatory():
    decision = check("Battle Through Time PC", "<p>Original box.</p>")
    assert decision.status == "rejected"
    assert decision.missing_groups == (("commodore", "c64", "c-64"),)


def test_empty_required_terms_add_no_platform_condition():
    assert check("Battle", "<p>Through Time</p>", required=()).status == "accepted"


def test_phrases_cannot_be_constructed_across_title_and_description():
    assert check("Battle Through C64", "<p>Time</p>", base='"Battle Through Time"').status == "rejected"
    assert check("Battle Through", "<p>Time for C64</p>", base='"Battle Through Time"').status == "rejected"
    assert check("A C64 game", "<p>Battle Through Time</p>", base='"Battle Through Time"').status == "accepted"
    assert check("Galaxy Commodore", "<p>64 Disk</p>", base="Galaxy Disk",
                 required=("commodore 64",)).status == "rejected"


def test_alternative_group_remains_an_or_condition():
    base = '(Slapshot, "Slap Shot")'
    assert check("Slap Shot C64", "<p>Original box</p>", base=base).status == "accepted"
    assert check("Slapshot C64", "<p>Original box</p>", base=base).status == "accepted"
    assert check("Slap C64", "<p>Shot</p>", base=base).status == "rejected"


def test_galaxy_disk_wolfenstein_false_positive_without_recommendations():
    decision = check("WOLFENSTEIN enemy TERRITORY online game solo cd pc cdrom pf",
                     "<p>gioco pc cd rom usato cd in ottime condizioni</p>", base="Galaxy Disk")
    assert decision.status == "rejected"
    assert ("galaxy",) in decision.missing_groups
    assert ("disk",) in decision.missing_groups


def test_only_visible_text_and_whole_words_count():
    decision = check("Battles through sometimes C64",
                     '<style>battle time</style><script>battle time</script>'
                     '<a href="https://example.com/battle/time">A game</a>')
    assert decision.status == "rejected"
    assert decision.missing_groups == (("battle",), ("time",))


def test_strict_recommendation_removal_and_its_setting():
    assert check(TITLE, HTML, base="Anirog").status == "rejected"
    assert check(TITLE, HTML, base="Anirog", filter_recommendations=False).status == "accepted"
    assert check(TITLE, HTML + "<p>Anirog release.</p>", base="Anirog").status == "accepted"


def run(monkeypatch, responses, queries=None, **kwargs):
    session = Session(responses)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    client = EbayClient("token", validate_search_terms=True, filter_recommendations=True, **kwargs)
    result = asyncio.run(client.run_queries(["EBAY_GB"], queries or [query()], {}))
    return result, client, session


@pytest.mark.parametrize("response", [
    Response(404), Response(401), detail_response(""),
    detail_response("<p></p>"), detail_response("<div>Battle Through Time C64"),
    detail_response(HTML.replace("www.ebay.co.uk", "example.com")),
    detail_response(rest_id="different"), Response(data={"shortDescription": "Battle Through Time C64"}),
])
def test_uncheckable_results_are_deferred_and_make_run_partial(monkeypatch, response):
    result, _, _ = run(monkeypatch, [search_response(), response])
    assert result.items == []
    assert result.recommendations.deferred == 1
    assert result.recommendations.uncertain == 0
    assert result.exit_code == 1
    assert "PARTIAL SEARCH" in result.summary
    assert "deferred=1" in result.summary


def test_exhausted_network_retries_defer_instead_of_accepting(monkeypatch):
    async def no_wait(_):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    result, client, _ = run(monkeypatch, [search_response(), aiohttp.ClientError(),
                                         aiohttp.ClientError(), aiohttp.ClientError()])
    assert not result.items
    assert result.recommendations.deferred == 1
    assert result.exit_code == 1
    assert client.description_calls == 3


def test_call_cap_defers_and_rate_limit_still_wins(monkeypatch):
    result, _, session = run(monkeypatch, [search_response()], max_search_calls=1)
    assert len(session.calls) == 1
    assert not result.items
    assert result.recommendations.deferred == 1
    assert result.exit_code == 1
    result, _, _ = run(monkeypatch, [search_response(), Response(429)])
    assert not result.items
    assert result.exit_code == 2
    assert "unchecked matches skipped" in result.summary


def test_title_only_validation_never_fetches_description(monkeypatch):
    result, client, session = run(monkeypatch, [search_response([
        item("Battle Through Time C64"), item("Battle C64", "v1|333333333333|0")])],
        search_in_description=False)
    assert [i.name for i in result.items] == ["Battle Through Time C64"]
    assert result.recommendations.accepted == 1
    assert result.recommendations.rejected == 1
    assert result.exit_code == 0
    assert client.description_calls == 0
    assert len(session.calls) == 1


def test_shared_item_can_pass_one_query_and_fail_another(monkeypatch):
    result, client, _ = run(monkeypatch, [search_response(), search_response(),
                                         detail_response("<p>Glider Rider C64</p>")],
                            queries=[query("Galaxy Disk"), query("Glider Rider")])
    assert [i.keywords for i in result.items] == ["Glider Rider"]
    assert client.description_calls == 1
    assert result.recommendations.rejected == 1
    assert result.recommendations.accepted == 1


def test_rejection_log_names_missing_words(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="fastebaysearch"):
        result, _, _ = run(monkeypatch, [search_response([item("Battle C64")]),
                                        detail_response("<p>An original game.</p>")])
    assert result.recommendations.rejected == 1
    assert "missing_positive_terms" in caplog.text
    assert "through" in caplog.text and "time" in caplog.text
    assert "206651330408" in caplog.text


@pytest.mark.parametrize("runner", [cli.run_app, cli.run_clean_search, cli.run_api_budget_estimate])
def test_unsupported_syntax_fails_before_side_effects(tmp_path, monkeypatch, capsys, runner):
    import inspect
    data = minimal_config()
    data["ebay_validate_search_terms"] = True
    data["ebay_search_keywords"]["base_terms"] = ["battle*"]
    path = write_config(tmp_path, data)

    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported validation syntax must fail before side effects")

    for name in ("setup_logging", "Database", "EbayAuth"):
        monkeypatch.setattr(cli, name, forbidden)
    result = runner(path, tmp_path)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    assert result == 1
    assert "unsupported positive keyword syntax" in capsys.readouterr().err


def test_builder_preserves_legacy_syntax_but_checks_strict_configuration():
    from fastebaysearch_app.config import ConfigError
    from test_query_builder import make_config
    config = make_config()
    config.search.base_terms[:] = ["battle*"]
    assert build_queries(config)[0].positive_groups is None
    with pytest.raises(ConfigError, match="unsupported positive keyword syntax"):
        build_queries(replace(config, ebay_validate_search_terms=True))
    config.search.base_terms[:] = ["Battle Through Time"]
    strict = replace(config, ebay_validate_search_terms=True)
    assert build_queries(config)[0].keywords == build_queries(strict)[0].keywords
    assert build_queries(strict)[0].positive_groups == (("battle",), ("through",), ("time",), ("canon", "nikon"))


def test_normal_workflow_defers_without_marking_seen_and_retries_on_later_search(tmp_path, monkeypatch):
    from urllib.parse import unquote
    from fastebaysearch_app.config import load_config
    from fastebaysearch_app.database import Database
    from fastebaysearch_app.models import SearchResult
    from fastebaysearch_app.workflow import Workflow

    class RoutedSession(Session):
        second_run = False

        def get(self, url, **kwargs):
            from fastebaysearch_app.ebay_client import SEARCH_URL, ITEM_URL
            if url == SEARCH_URL:
                return search_response([item("Battle Through Time C64", "v1|101|0"),
                                        item("Battle C64", "v1|102|0"), item("Battle C64", "v1|103|0"),
                                        item("Battle C64", "v1|104|0")])
            rest_id = unquote(url.removeprefix(ITEM_URL))
            if rest_id == "v1|103|0":
                return detail_response("<p>Through Time</p>", rest_id) if self.second_run else Response(404)
            return detail_response("<p>An original cassette.</p>", rest_id)

    class Client(EbayClient):
        async def get_exchange_rates(self):
            return {}

    class Report:
        sent = []

        def send(self, items, person_name, run_summary=""):
            self.sent = list(items)
            return self.sent

    data = minimal_config()
    data.update(use_html_report=True, html_report_max_per_run=100, ebay_validate_search_terms=True)
    data["ebay_search_keywords"] = {"base_terms": ["Battle Through Time"], "required_terms": ["c64"]}
    config = load_config(write_config(tmp_path, data), tmp_path)
    database = Database(config.db_path)
    database.ensure_schema()
    old = SearchResult("Battle Through Time", "Old queued listing", "EBAY_GB", "10 EUR", "104", "", "seller", "", "")
    database.claim_new_results([old], enqueue_html=True)
    session = RoutedSession([])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    report = Report()
    client = Client("token", validate_search_terms=True)

    def workflow():
        return Workflow(config, database, client, build_queries(config), html_report_notifier=report)

    first = asyncio.run(workflow().run())
    assert first.exit_code == 1
    assert [i.item_id for i in first.new_results] == ["101"]
    assert {i.item_id for i in report.sent} == {"101", "104"}
    with database.connect() as connection:
        assert dict(connection.execute("SELECT item_id, title FROM ebayids")) == {
            "101": "Battle Through Time C64", "104": "Old queued listing"}
    session.second_run = True
    second = asyncio.run(workflow().run())
    assert second.exit_code == 0
    assert [i.item_id for i in second.new_results] == ["103"]
    assert [i.item_id for i in report.sent] == ["103"]


@pytest.mark.parametrize("no_results", [False, True])
def test_clean_search_reports_deferred_checks_without_database_access(tmp_path, monkeypatch, no_results):
    data = minimal_config()
    data.update(ebay_validate_search_terms=True, html_report_max_per_run=100)
    data["ebay_search_keywords"] = {"base_terms": ["Battle Through Time"], "required_terms": ["c64"]}
    config_path = write_config(tmp_path, data)
    items = [item("Battle C64", "v1|103|0")]
    if not no_results:
        items.append(item("Battle Through Time C64", "v1|101|0"))
    session = Session([search_response(items), Response(404)])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)

    class Client(EbayClient):
        async def get_exchange_rates(self):
            return {}

    report = type("Report", (), {"sent": None, "summary": None})()

    class Notifier:
        def __init__(self, *args):
            pass

        def send(self, items, person_name, run_summary=""):
            report.sent = list(items)
            report.summary = run_summary
            return report.sent

        def send_status(self, summary, person_name):
            report.summary = summary
            return True

    async def make_client(config, logger):
        assert config.ebay_validate_search_terms is True
        return Client("token", validate_search_terms=config.ebay_validate_search_terms)

    monkeypatch.setattr(cli, "_make_client", make_client)
    monkeypatch.setattr(cli, "HtmlReportNotifier", Notifier)
    monkeypatch.setattr(cli, "setup_logging", lambda *args: logging.getLogger("test-validation"))
    monkeypatch.setattr(cli, "Database", lambda *a, **kw: pytest.fail("Clean search must not access database"))
    assert asyncio.run(cli.run_clean_search(config_path, tmp_path)) == 1
    assert [i.item_id for i in report.sent] == ([] if no_results else ["101"])
    assert "PARTIAL SEARCH" in report.summary
    assert "deferred=1" in report.summary
    assert not (tmp_path / "items.db").exists()
