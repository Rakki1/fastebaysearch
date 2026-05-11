from __future__ import annotations

from .utils import safe_url


EBAY_MARKETPLACE_DOMAINS = {
    "EBAY_AT": "www.ebay.at",
    "EBAY_AU": "www.ebay.com.au",
    "EBAY_BE": "www.ebay.be",
    "EBAY_CA": "www.ebay.ca",
    "EBAY_CH": "www.ebay.ch",
    "EBAY_DE": "www.ebay.de",
    "EBAY_ES": "www.ebay.es",
    "EBAY_FR": "www.ebay.fr",
    "EBAY_GB": "www.ebay.co.uk",
    "EBAY_HK": "www.ebay.com.hk",
    "EBAY_IE": "www.ebay.ie",
    "EBAY_IT": "www.ebay.it",
    "EBAY_MY": "www.ebay.com.my",
    "EBAY_MOTORS_US": "www.ebay.com",
    "EBAY_NL": "www.ebay.nl",
    "EBAY_PH": "www.ebay.ph",
    "EBAY_PL": "www.ebay.pl",
    "EBAY_SG": "www.ebay.com.sg",
    "EBAY_US": "www.ebay.com",
}


def build_item_web_url(item_id: str, listing_marketplace_id: str, fallback_url: object = "") -> str:
    domain = EBAY_MARKETPLACE_DOMAINS.get(str(listing_marketplace_id).upper())
    if item_id and domain:
        return f"https://{domain}/itm/{item_id}"
    return safe_url(str(fallback_url).split("?")[0])
