from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


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


@dataclass
class QueryOutcome:
    site: str
    query: SearchQuery
    status: Literal["success", "failed", "truncated", "rate_limited", "cancelled"] = "cancelled"
    raw_items: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    total: int | None = None
    error: str = ""


@dataclass
class SearchRun:
    items: list[SearchResult] = field(default_factory=list)
    outcomes: list[QueryOutcome] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if any(outcome.status == "rate_limited" for outcome in self.outcomes):
            return 2
        return int(any(outcome.status != "success" for outcome in self.outcomes))

    @property
    def summary(self) -> str:
        counts = {status: sum(o.status == status for o in self.outcomes)
                  for status in ("success", "failed", "truncated", "rate_limited", "cancelled")}
        state = "PARTIAL SEARCH" if self.exit_code else "Search complete"
        return state + ": " + ", ".join(f"{status}={count}" for status, count in counts.items())
