from __future__ import annotations

from .models import SearchQuery
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
            candidate = f'{full_query} -"{clean_exclude}"'
            if len(candidate) <= max_q_len:
                full_query = candidate
            else:
                break

        queries.append(SearchQuery(keywords=full_query, base_name=base))

    return queries
