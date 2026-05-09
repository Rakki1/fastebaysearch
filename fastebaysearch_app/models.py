from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchQuery:
    keywords: str
    base_name: str


@dataclass(frozen=True)
class SearchResult:
    keywords: str
    name: str
    ebay_site: str
    price: str
    item_id: str
    link: str
    seller: str
    starts: str
    ends: str
    image: str = ""
