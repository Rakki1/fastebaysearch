import json
import asyncio
from dataclasses import replace

import pytest

from fastebaysearch_app.database import Database
from fastebaysearch_app.ebay_client import parse_search_results
from fastebaysearch_app.ebay_client import EbayClient
from fastebaysearch_app.models import SearchQuery
from fastebaysearch_app.recommendation_filter import positive_groups
from fastebaysearch_app.ebay_urls import build_item_web_url
from fastebaysearch_app.notifications.email import render_email_html
from fastebaysearch_app.notifications.html_report import HtmlReportNotifier
from fastebaysearch_app.notifications.telegram import build_caption
from fastebaysearch_app.workflow import dedupe_by_item_id


def result(location=None, marketplace="EBAY_GB"):
    raw = {"itemId": "v1|1234567890|0", "listingMarketplaceId": marketplace,
           "itemWebUrl": "https://www.ebay.co.uk/itm/1234567890?hash=abc", "title": "Anirog C64",
           "price": {"value": "20", "currency": "GBP"}, "buyingOptions": ["FIXED_PRICE"],
           "itemLocation": location}
    return replace(parse_search_results("Anirog", [raw], {})[0], source_site="EBAY_GB")


@pytest.mark.parametrize("country,domain", [
    ("DE", "www.ebay.de"), ("GB", "www.ebay.co.uk"), ("US", "www.ebay.com"),
    ("IT", "www.ebay.it"), ("FR", "www.ebay.fr"), ("AU", "www.ebay.com.au"),
    ("CA", "www.ebay.ca"), (" de ", "www.ebay.de"),
])
def test_location_country_controls_link_even_for_uk_listing_and_search(country, domain):
    item = result({"country": country})
    assert item.location_country == country.strip().upper()
    assert item.link == f"https://{domain}/itm/1234567890"
    assert item.ebay_site == "EBAY_GB"
    assert item.source_site == "EBAY_GB"


@pytest.mark.parametrize("location", [None, {}, [], "DE", {"country": None}, {"country": 42},
                                      {"country": {"country": "DE"}}, {"country": "<DE>"}])
def test_missing_or_invalid_location_uses_listing_marketplace(location):
    item = result(location, "EBAY_US")
    assert item.location_country == ""
    assert item.link == "https://www.ebay.com/itm/1234567890"


def test_country_without_supported_ebay_site_is_recorded_but_uses_listing_site():
    item = result({"country": "FI"}, "EBAY_DE")
    assert item.location_country == "FI"
    assert item.link == "https://www.ebay.de/itm/1234567890"
    item = result({"country": "FI"}, "Unknown")
    assert item.link == "https://www.ebay.co.uk/itm/1234567890"


def test_known_location_works_without_listing_marketplace_or_url():
    assert build_item_web_url("12345", "Unknown", "", location_country="DE") == "https://www.ebay.de/itm/12345"
    assert build_item_web_url("12345", "Unknown", "javascript:alert(1)", location_country="FI") == ""


@pytest.mark.parametrize("reverse", [False, True])
def test_dedup_keeps_native_price_and_merges_location_from_foreign_search(reverse):
    domestic = result()
    foreign = replace(result({"country": "DE"}), source_site="EBAY_US", price="25 USD")
    items = [domestic, foreign]
    merged = dedupe_by_item_id(list(reversed(items)) if reverse else items)[0]
    assert merged.location_country == "DE"
    assert merged.link == "https://www.ebay.de/itm/1234567890"
    assert merged.price == domestic.price
    assert merged.source_site == domestic.source_site
    assert merged.ebay_site == domestic.ebay_site


def test_database_preserves_location_for_all_pending_channels_and_later_missing_data(tmp_path):
    db = Database(tmp_path / "items.db")
    db.ensure_schema()
    item = result({"country": "DE"})
    db.claim_new_results([item], enqueue_html=True)
    db.claim_new_results([result()], enqueue_html=True)
    email = db.pending_email_notifications()
    telegram = db.claim_pending_telegram_notifications()
    for pending in [email, telegram]:
        assert pending[0].location_country == "DE"
        assert pending[0].link == item.link
    html = HtmlReportNotifier(tmp_path, 100)
    assert db.deliver_html_notifications(html, 100, "User")
    text = next(tmp_path.glob("*.html")).read_text(encoding="utf-8")
    assert "<th>Location</th>" in text and "<td>DE</td>" in text
    assert item.link in text
    with db.connect() as connection:
        snapshot = json.loads(connection.execute("SELECT snapshot FROM item_observations").fetchone()[0])
    assert snapshot["location_country"] == "DE"


def test_new_location_refreshes_old_pending_link_without_creating_change_alert(tmp_path):
    db = Database(tmp_path / "items.db")
    db.ensure_schema()
    old = result()
    db.claim_new_results([old])
    assert db.pending_email_notifications()[0].link == old.link
    located = result({"country": "DE"})
    assert db.claim_new_results([located]) == []
    assert db.pending_email_notifications()[0].link == located.link
    assert db.pending_email_notifications()[0].location_country == "DE"
    db.mark_email_succeeded([located])
    db.claim_new_results([replace(located, location_country="FR", link="https://www.ebay.fr/itm/1234567890")])
    assert db.pending_email_notifications() == []


def test_location_survives_change_events_and_old_event_snapshots_remain_readable(tmp_path):
    db = Database(tmp_path / "items.db")
    db.ensure_schema()
    old = result({"country": "DE"})
    db.claim_new_results([old])
    db.mark_email_succeeded([old])
    db.claim_new_results([replace(old, fixed_amount="15", price="15 GBP")])
    event = db.pending_email_notifications()[0]
    assert event.event_id
    assert event.location_country == "DE"
    assert event.link == old.link
    with db.connect() as connection:
        snapshot = json.loads(connection.execute("SELECT snapshot FROM change_events").fetchone()[0])
        snapshot.pop("location_country")
        connection.execute("UPDATE change_events SET snapshot=?", (json.dumps(snapshot),))
    assert db.pending_email_notifications()[0].location_country == ""


def test_notifications_show_location_separately_from_listing_marketplace():
    item = result({"country": "DE"})
    text = render_email_html([item], "User")
    assert "<td>EBAY_GB</td>" in text
    assert "<td>DE</td>" in text
    assert item.link in text
    caption = build_caption(item)
    assert "Location: DE" in caption
    assert item.link in caption


def test_missing_search_location_reuses_description_metadata_without_extra_calls(monkeypatch):
    import aiohttp
    from http_fakes import Response, Session
    raw = {"itemId": "v1|1234567890|0", "title": "C64 game", "listingMarketplaceId": "EBAY_GB"}
    session = Session([
        Response(data={"total": 1, "itemSummaries": [raw]}),
        Response(data={"itemId": raw["itemId"], "description": "<p>Anirog software</p>",
                       "itemLocation": {"country": "DE"}}),
    ])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    client = EbayClient("token", validate_search_terms=True)
    query = SearchQuery("Anirog C64", "Anirog", positive_groups("Anirog", ["c64"]))
    run = asyncio.run(client.run_queries(["EBAY_GB"], [query], {}))
    assert run.exit_code == 0
    assert len(session.calls) == 2
    assert client.description_calls == 1
    assert run.items[0].location_country == "DE"
    assert run.items[0].link == "https://www.ebay.de/itm/1234567890"
