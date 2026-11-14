from __future__ import annotations

from .config import ConfigError
from .models import SearchQuery
from .recommendation_filter import positive_groups
from .utils import normalize_terms


def build_queries(config, max_q_len: int = 100) -> list[SearchQuery]:
    required_terms = config.search.required_terms
    base_terms = config.search.base_terms
    any_of_terms = "(" + ", ".join(f'"{term}"' for term in required_terms) + ")" if required_terms else ""
    exclude_terms = normalize_terms(config.exclude_terms)

    queries: list[SearchQuery] = []
    for base in base_terms:
        full_query = f"{any_of_terms} {base}".strip()

        for exclude in exclude_terms:
            clean_exclude = exclude.lstrip("-").strip('"')
            full_query = f'{full_query} -"{clean_exclude}"'

        if len(full_query) > max_q_len:
            raise ConfigError(
                f"Query for {base!r} is {len(full_query)} characters (maximum {max_q_len}): "
                f"{full_query}. Shorten the terms or split the search configuration."
            )

        groups = positive_groups(base, required_terms)
        if getattr(config, "ebay_validate_search_terms", False) and groups is None:
            raise ConfigError(
                f"Cannot validate search terms for {base!r}: unsupported positive keyword syntax. "
                "Use ordinary words, quoted phrases, or comma-separated alternatives in parentheses."
            )
        queries.append(SearchQuery(keywords=full_query, base_name=base, positive_groups=groups))

    return queries
