import asyncio
import sqlite3
from contextlib import contextmanager
from dataclasses import replace

import aiohttp
import pytest

from fastebaysearch_app.database import Database
from fastebaysearch_app.description_cache import (DescriptionCache, DescriptionEntry, summary_fingerprint,
                                                  validation_key)
from fastebaysearch_app.ebay_client import EbayClient
from fastebaysearch_app.models import SearchQuery
from fastebaysearch_app.recommendation_filter import Decision, positive_groups
from test_search_reliability import Response, Session

NOW = 1800000000.0
REST = "v1|1234567890|0"


def raw(**changes):
    item = {"itemId": REST, "title": "A cassette game C64", "listingMarketplaceId": "EBAY_GB",
            "price": {"value": "20", "currency": "GBP"}, "buyingOptions": ["FIXED_PRICE"]}
    item.update(changes)
    return item


def query(base="Anirog"):
    return SearchQuery(base + " C64", base, positive_groups(base, ["c64"]))


def summary(items=None):
    items = [raw()] if items is None else items
    return Response(data={"total": len(items), "itemSummaries": items})


def detail(html="<p>Anirog software for C64</p>", rest=REST, country="DE"):
    return Response(data={"itemId": rest, "description": html, "itemLocation": {"country": country}})


@pytest.fixture
def db(tmp_path):
    value = Database(tmp_path / "items.db")
    value.ensure_schema()
    return value


@pytest.fixture
def clock(monkeypatch):
    value = [NOW]
    monkeypatch.setattr("fastebaysearch_app.ebay_client.time.time", lambda: value[0])
    return value


def run(monkeypatch, db, responses, queries=None, site="EBAY_GB", cache=None, **kwargs):
    session = Session(responses)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    client = EbayClient("token", validate_search_terms=True, filter_recommendations=True, **kwargs)
    cache = cache if cache is not None else DescriptionCache(db.connect, "app")
    client.configure_description_cache(cache)
    result = asyncio.run(client.run_queries([site], queries or [query()], {}))
    return result, client, session


def test_second_run_after_restart_uses_persisted_decision_and_fresh_price(monkeypatch, db, clock):
    first, _, _ = run(monkeypatch, db, [summary(), detail()])
    assert first.browse_calls == 2
    assert first.recommendations.description_cache_misses == 1
    second, client, session = run(monkeypatch, db, [summary([raw(price={"value": "15", "currency": "GBP"})])])
    assert second.browse_calls == 1
    assert client.description_calls == 0
    assert len(session.calls) == 1
    assert second.recommendations.description_cache_hits == 1
    assert second.recommendations.validation_cache_hits == 1
    assert second.items[0].price == "15.00 GBP"
    assert second.items[0].location_country == "DE"
    assert second.items[0].link == "https://www.ebay.de/itm/1234567890"


def test_rejected_items_are_cached_without_marking_them_seen(monkeypatch, db, clock):
    first, _, _ = run(monkeypatch, db, [summary(), detail("<p>Another game for C64</p>")])
    assert first.items == []
    second, _, _ = run(monkeypatch, db, [summary()])
    assert second.items == []
    assert second.exit_code == 0
    assert second.recommendations.validation_cache_hits == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ebayids").fetchone()[0] == 0


def test_changed_query_rechecks_cached_html_without_another_api_call(monkeypatch, db, clock):
    run(monkeypatch, db, [summary(), detail("<p>Anirog Galaxy Disk C64</p>")])
    second, _, _ = run(monkeypatch, db, [summary()], [query("Galaxy Disk")])
    assert second.items[0].keywords == "Galaxy Disk"
    assert second.browse_calls == 1
    assert second.recommendations.validation_cache_hits == 0
    assert second.recommendations.description_cache_hits == 1

    def forbidden(*args, **kwargs):
        pytest.fail("A persisted decision should not need HTML parsing again")

    monkeypatch.setattr("fastebaysearch_app.ebay_client.check_description", forbidden)
    third, _, _ = run(monkeypatch, db, [summary()], [query("Galaxy Disk")])
    assert third.recommendations.validation_cache_hits == 1


def test_cache_hit_does_not_extend_description_expiry(monkeypatch, db, clock):
    run(monkeypatch, db, [summary(), detail()])
    clock[0] += 23 * 3600
    assert run(monkeypatch, db, [summary()])[0].description_calls == 0
    clock[0] = NOW + 24 * 3600
    expired, _, _ = run(monkeypatch, db, [summary(), detail("<p>A different C64 publisher</p>")])
    assert expired.description_calls == 1
    assert expired.items == []


@pytest.mark.parametrize("changes", [
    {"title": "A renamed cassette C64"}, {"sellerItemRevision": "2"}, {"conditionId": "4000"},
    {"categories": [{"categoryId": "42"}]},
])
def test_changed_summary_invalidates_cached_description(monkeypatch, db, clock, changes):
    run(monkeypatch, db, [summary(), detail()])
    result, _, _ = run(monkeypatch, db, [summary([raw(**changes)]), detail()])
    assert result.description_calls == 1


def test_bid_and_buying_options_stay_fresh_without_invalidating_description(monkeypatch, db, clock):
    run(monkeypatch, db, [summary(), detail()])
    auction = raw(currentBidPrice={"value": "8", "currency": "GBP"}, buyingOptions=["AUCTION"])
    auction.pop("price")
    result, _, _ = run(monkeypatch, db, [summary([auction])])
    assert result.description_calls == 0
    assert result.items[0].price == "Current bid: 8.00 GBP"
    assert result.items[0].buying_options == ("AUCTION",)


def test_same_rest_id_is_separate_for_marketplaces_and_variations(monkeypatch, db, clock):
    run(monkeypatch, db, [summary(), detail()])
    different_site, _, _ = run(monkeypatch, db, [summary(), detail()], site="EBAY_DE")
    assert different_site.description_calls == 1
    variant = "v1|1234567890|42"
    different_variant, _, _ = run(monkeypatch, db, [summary([raw(itemId=variant)]), detail(rest=variant)])
    assert different_variant.description_calls == 1
    different_app = DescriptionCache(db.connect, "another-app")
    assert run(monkeypatch, db, [summary(), detail()], cache=different_app)[0].description_calls == 1


@pytest.mark.parametrize("failure", [Response(404), detail("<div>Anirog C64"), detail("")])
def test_failures_and_uncertain_checks_are_not_cached(monkeypatch, db, clock, failure):
    first, _, _ = run(monkeypatch, db, [summary(), failure])
    assert first.exit_code == 1
    assert first.recommendations.deferred == 1
    second, _, _ = run(monkeypatch, db, [summary(), detail()])
    assert second.exit_code == 0
    assert second.description_calls == 1


def test_valid_cache_entry_is_reused_even_when_description_call_budget_is_gone(monkeypatch, db, clock):
    run(monkeypatch, db, [summary(), detail()])
    result, _, _ = run(monkeypatch, db, [summary()], max_search_calls=1)
    assert result.exit_code == 0
    assert result.recommendations.budget_exhausted is False
    assert result.items


def test_validation_mode_changes_recompute_decision_from_cached_description(monkeypatch, db, clock):
    run(monkeypatch, db, [summary(), detail("<p>Anirog C64</p>")])
    cache = DescriptionCache(db.connect, "app")
    fingerprint = summary_fingerprint(raw())
    old_rule = validation_key(query().positive_groups, True, True)
    assert cache.get_decision("EBAY_GB", REST, fingerprint, old_rule, NOW)
    assert cache.get_decision("EBAY_GB", REST, fingerprint,
                              validation_key(query().positive_groups, True, False), NOW) is None


def test_disabling_cache_does_not_reuse_or_store_entries(monkeypatch, db, clock):
    disabled = DescriptionCache(db.connect, "app", ttl_hours=0)
    for _ in range(2):
        result, _, _ = run(monkeypatch, db, [summary(), detail()], cache=disabled)
        assert result.description_calls == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM description_cache").fetchone()[0] == 0


def test_cache_io_failure_falls_back_to_real_verification(monkeypatch, db, clock):
    @contextmanager
    def broken():
        raise sqlite3.OperationalError("test failure")
        yield

    result, _, _ = run(monkeypatch, db, [summary(), detail()], cache=DescriptionCache(broken, "app"))
    assert result.exit_code == 0
    assert result.items
    assert result.recommendations.description_cache_uncached_entries == 1


def test_newer_concurrent_description_is_not_overwritten(db):
    cache = DescriptionCache(db.connect, "app")
    fingerprint = summary_fingerprint(raw())
    rule = validation_key(query().positive_groups, True, True)
    accepted = Decision("accepted", "test")
    assert cache.put("EBAY_GB", REST, DescriptionEntry(fingerprint, NOW, "newer"), rule, accepted)
    assert not cache.put("EBAY_GB", REST, DescriptionEntry(fingerprint, NOW - 1, "older"), rule, accepted)
    assert cache.get_description("EBAY_GB", REST, fingerprint, NOW).description == "newer"


def test_pruning_expired_data_preserves_fresh_data_and_other_applications(db):
    cache = DescriptionCache(db.connect, "app")
    other = DescriptionCache(db.connect, "other")
    accepted = Decision("accepted", "test")
    entry = DescriptionEntry("fp", NOW, "<p>Test</p>")
    cache.put("EBAY_GB", REST, entry, "rule", accepted)
    other.put("EBAY_GB", REST, entry, "rule", accepted)
    cache.prune(NOW + 86400)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM description_cache").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM validation_cache").fetchone()[0] == 1


def test_fresh_summary_country_takes_precedence_over_cached_country(monkeypatch, db, clock):
    run(monkeypatch, db, [summary(), detail(country="DE")])
    result, _, _ = run(monkeypatch, db, [summary([raw(itemLocation={"country": "FR"})])])
    assert result.description_calls == 0
    assert result.items[0].location_country == "FR"
    assert result.items[0].link == "https://www.ebay.fr/itm/1234567890"


@pytest.mark.parametrize("ttl,expected_calls", [(24, [2, 1]), (0, [2, 2])])
def test_normal_workflow_configures_cache_across_process_restarts(monkeypatch, db, clock, ttl, expected_calls):
    from fastebaysearch_app.workflow import Workflow
    from test_query_builder import make_config

    cfg = replace(make_config(), ebay_sites=["EBAY_GB"], ebay_check_api_quota=False,
                  ebay_description_cache_ttl_hours=ttl)
    db.replace_exchange_rates({"GBP": 1})
    for calls in expected_calls:
        session = Session([summary()] + ([detail()] if calls == 2 else []))
        monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
        client = EbayClient("token", validate_search_terms=True, filter_recommendations=True)
        result = asyncio.run(Workflow(cfg, db, client, [query()]).run())
        assert result.exit_code == 0
        assert result.search_run.browse_calls == calls
        assert result.search_run.description_cache_ttl_hours == ttl
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ebayids").fetchone()[0] == 1


def test_clean_search_always_fetches_fresh_description_without_database(tmp_path, monkeypatch):
    from fastebaysearch_app import cli
    from test_cli import minimal_config, setup_clean_search_fakes, write_config

    data = minimal_config()
    data.update(ebay_sites=["EBAY_GB"], ebay_search_in_description=True,
                ebay_filter_recommendations=True, ebay_validate_search_terms=True,
                ebay_description_cache_ttl_hours=24)
    data["ebay_search_keywords"] = {"base_terms": ["Anirog"], "required_terms": ["C64"]}
    path = write_config(tmp_path, data)
    setup_clean_search_fakes(monkeypatch)
    monkeypatch.setattr(cli, "EbayClient", EbayClient)
    monkeypatch.setattr(cli, "Database", lambda *a, **kw: pytest.fail("Clean search used database"))
    monkeypatch.setattr(DescriptionCache, "get_decision", lambda *a: pytest.fail("Clean search used cache"))

    async def rates(self):
        return {"GBP": 1}

    monkeypatch.setattr(EbayClient, "get_exchange_rates", rates)
    session = Session([summary(), detail()])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    assert asyncio.run(cli.run_clean_search(path, tmp_path)) == 0
    assert len(session.calls) == 2
    assert not (tmp_path / "items.db").exists()
