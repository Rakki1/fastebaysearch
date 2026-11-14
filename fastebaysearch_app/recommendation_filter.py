"""Conservative detection of matches contributed only by known recommendation HTML."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .ebay_urls import EBAY_MARKETPLACE_DOMAINS

PositiveGroups = tuple[tuple[str, ...], ...]


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def positive_groups(base: str, required: list[str]) -> PositiveGroups | None:
    """Recognize our supported keyword subset; never guess at unfamiliar syntax."""
    token = re.compile(r'\s*(-?"[^"]+"|[(),]|[^\s(),"]+)')
    tokens = []
    pos = 0
    while pos < len(base.rstrip()):
        match = token.match(base, pos)
        if not match:
            return None
        tokens.append(match[1])
        pos = match.end()

    def literal(value):
        value = value.strip('"')
        return normalize(value) if re.fullmatch(r"[\w\s'’\-]+", value) and value.strip() else None

    groups = []
    i = 0
    while i < len(tokens):
        term = tokens[i]
        if term in ("OR", "AND", "NOT"):
            return None
        if term == "(":
            alternatives = []
            i += 1
            while True:
                if i >= len(tokens) or tokens[i].startswith("-"):
                    return None
                value = literal(tokens[i])
                if value is None:
                    return None
                alternatives.append(value)
                i += 1
                if i >= len(tokens):
                    return None
                if tokens[i] == ")":
                    break
                if tokens[i] != ",":
                    return None
                i += 1
            groups.append(tuple(alternatives))
        elif term.startswith("-"):
            if literal(term[1:]) is None:
                return None
        else:
            value = literal(term)
            if value is None:
                return None
            groups.append((value,))
        i += 1
    if required:
        values = [literal(term) for term in required]
        if any(value is None for value in values):
            return None
        groups.append(tuple(values))
    return tuple(groups) if groups else None


def matches(text: str, groups: PositiveGroups) -> bool:
    text = normalize(text)
    return all(any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text)
                   for term in group) for group in groups)


class DivBalance(HTMLParser):
    """A repaired, unclosed div could otherwise swallow real description text."""
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.valid = True

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag == "div":
            self.depth -= 1
            if self.depth < 0:
                self.valid = False


@dataclass(frozen=True)
class Decision:
    status: str
    reason: str


def check_description(title: str, description: str, item_id: str,
                      groups: PositiveGroups) -> Decision:
    balance = DivBalance()
    balance.feed(description)
    balance.close()
    if not balance.valid or balance.depth:
        return Decision("uncertain", "unbalanced_description_html")
    soup = BeautifulSoup(description, "html.parser")
    for tag in soup.select("script, style, template"):
        tag.decompose()
    blocks = soup.select("div.wi-recommendations")
    if not blocks:
        return Decision("uncertain", "no_supported_recommendations")
    for block in blocks:
        cards = block.select(".wi-recommendation")
        if not cards:
            return Decision("uncertain", "unrecognized_recommendations")
        for card in cards:
            links = card.select("a[href]")
            if not links:
                return Decision("uncertain", "unrecognized_recommendation_link")
            for link in links:
                url = urlparse(link["href"])
                match = re.fullmatch(r"/itm/(?:[^/]+/)?(\d+)/?", url.path)
                if (url.scheme not in ("http", "https")
                        or url.hostname not in EBAY_MARKETPLACE_DOMAINS.values()
                        or not match or match[1] == item_id):
                    return Decision("uncertain", "unrecognized_recommendation_link")
        # Do not remove an otherwise known wrapper that also contains real prose.
        remainder = BeautifulSoup(str(block), "html.parser")
        for known in remainder.select(".wi-recommendations-header, .wi-recommendation"):
            if known.parent is not None:
                known.decompose()
        if remainder.get_text(" ", strip=True):
            return Decision("uncertain", "unexpected_text_in_recommendations")
    original = title + " " + soup.get_text(" ", strip=True)
    if not matches(original, groups):
        return Decision("uncertain", "original_text_does_not_match")
    for block in blocks:
        if block.parent is not None:
            block.decompose()
    cleaned = title + " " + soup.get_text(" ", strip=True)
    if not normalize(soup.get_text(" ", strip=True)):
        return Decision("uncertain", "empty_cleaned_description")
    if matches(cleaned, groups):
        return Decision("accepted", "matches_without_recommendations")
    return Decision("rejected", "matches_only_with_recommendations")
