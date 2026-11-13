from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import requests

TAXONOMY_URL = "https://api.ebay.com/commerce/taxonomy/v1"
TRANSIENT_STATUSES = {500, 502, 503, 504}


class TaxonomyError(RuntimeError):
    pass


class TaxonomyRateLimit(TaxonomyError):
    pass


@dataclass(frozen=True)
class CategorySuggestion:
    category_id: str
    path: str


def _category(value):
    if not isinstance(value, dict):
        raise TaxonomyError("Invalid category in Taxonomy response")
    category_id = value.get("categoryId")
    name = value.get("categoryName")
    if (not isinstance(category_id, str) or not re.fullmatch(r"[0-9]+", category_id)
            or int(category_id) <= 0 or not isinstance(name, str) or not name.strip()):
        raise TaxonomyError("Invalid category in Taxonomy response")
    # Keep API-provided labels on one line and remove terminal control characters.
    return category_id, " ".join("".join(c if c.isprintable() else " " for c in name).split())


def parse_suggestions(data: dict) -> list[CategorySuggestion]:
    entries = data.get("categorySuggestions")
    if not isinstance(entries, list):
        raise TaxonomyError("Missing categorySuggestions in Taxonomy response")
    result = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise TaxonomyError("Invalid category suggestion")
        category_id, name = _category(entry.get("category"))
        ancestors = entry.get("categoryTreeNodeAncestors", [])
        if not isinstance(ancestors, list):
            raise TaxonomyError("Invalid category ancestors")
        path = []
        for ancestor in ancestors:
            if not isinstance(ancestor, dict) or type(ancestor.get("categoryTreeNodeLevel")) is not int:
                raise TaxonomyError("Invalid category ancestor level")
            ancestor_id, ancestor_name = _category(ancestor)
            path.append((ancestor["categoryTreeNodeLevel"], f"{ancestor_name} [{ancestor_id}]"))
        labels = [label for _, label in sorted(path)] + [f"{name} [{category_id}]"]
        if category_id not in seen:
            result.append(CategorySuggestion(category_id, " > ".join(labels)))
            seen.add(category_id)
    return result


class TaxonomyClient:
    def __init__(self, auth):
        self.auth = auth
        self.token = auth.get_access_token()
        self.refreshed = False

    def _get(self, session, path, params):
        retries = 0
        while True:
            try:
                response = session.get(
                    TAXONOMY_URL + path, params=params,
                    headers={"Authorization": "Bearer " + self.token}, timeout=20,
                )
            except requests.RequestException as exc:
                reason = f"Network error: {type(exc).__name__}"
            else:
                try:
                    status = response.status_code
                    if status == 401 and not self.refreshed:
                        self.refreshed = True
                        self.token = self.auth.get_access_token(force_refresh=True)
                        continue
                    if status == 429:
                        raise TaxonomyRateLimit("HTTP 429: Taxonomy rate limit")
                    if status == 204:
                        return {"categorySuggestions": []}
                    if status == 200:
                        try:
                            data = response.json()
                        except ValueError:
                            raise TaxonomyError("Invalid JSON in Taxonomy response") from None
                        if not isinstance(data, dict) or data.get("errors"):
                            raise TaxonomyError("Invalid Taxonomy response or API error payload")
                        return data
                    if status not in TRANSIENT_STATUSES:
                        raise TaxonomyError(f"HTTP {status}: Taxonomy request failed")
                    reason = f"HTTP {status}"
                finally:
                    response.close()
            if retries >= 2:
                raise TaxonomyError(reason)
            retries += 1
            time.sleep(retries)

    def find_categories(self, session, site: str, query: str) -> list[CategorySuggestion]:
        data = self._get(session, "/get_default_category_tree_id", {"marketplace_id": site})
        tree = data.get("categoryTreeId")
        if not isinstance(tree, str) or not re.fullmatch(r"[0-9]+", tree):
            raise TaxonomyError("Invalid categoryTreeId in Taxonomy response")
        data = self._get(session, f"/category_tree/{tree}/get_category_suggestions", {"q": query})
        return parse_suggestions(data)


def print_categories(results: dict[str, list[CategorySuggestion]]) -> None:
    example = {}
    for site, suggestions in results.items():
        print(f"\n{site}")
        if not suggestions:
            print("  No category suggestions found.")
            continue
        for suggestion in suggestions:
            print(f"  {suggestion.path}")
        example[site] = [suggestions[0].category_id]
    if example:
        print("\nConfiguration example using only the first suggestion for each successful marketplace:")
        print(json.dumps({"ebay_category_ids": example}, ensure_ascii=True, indent=2))
        print("Choose the IDs you want before copying. Parent IDs in the paths give broader results.")
        print("Missing marketplaces are unrestricted; review them before applying a partial example.")
    print("Configuration was not changed. Suggestions are ranked by eBay, not a complete category list.")
