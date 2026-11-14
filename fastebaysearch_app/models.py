from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class SearchQuery:
    keywords: str
    base_name: str
    positive_groups: tuple[tuple[str, ...], ...] | None = None


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
    buying_options: tuple[str, ...] = ()
    fixed_amount: str = ""
    fixed_currency: str = ""
    bid_amount: str = ""
    bid_currency: str = ""
    source_site: str = ""
    event_id: int = 0
    change_description: str = ""

    @property
    def notification_key(self):
        return (self.item_id, self.event_id)


@dataclass
class QueryOutcome:
    site: str
    query: SearchQuery
    status: Literal["success", "failed", "truncated", "rate_limited", "cancelled"] = "cancelled"
    raw_items: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    total: int | None = None
    error: str = ""
    category_id: str | None = None


@dataclass
class RecommendationStats:
    accepted: int = 0
    rejected: int = 0
    uncertain: int = 0
    description_calls: int = 0
    rate_limited: bool = False

    @property
    def summary(self) -> str:
        return (f"Recommendation check: accepted={self.accepted}, rejected={self.rejected}, "
                f"uncertain={self.uncertain}, description_calls={self.description_calls}")


@dataclass
class SearchRun:
    items: list[SearchResult] = field(default_factory=list)
    outcomes: list[QueryOutcome] = field(default_factory=list)
    recommendations: RecommendationStats | None = None

    @property
    def exit_code(self) -> int:
        if (any(outcome.status == "rate_limited" for outcome in self.outcomes)
                or (self.recommendations and self.recommendations.rate_limited)):
            return 2
        return int(any(outcome.status != "success" for outcome in self.outcomes))

    @property
    def summary(self) -> str:
        counts = {status: sum(o.status == status for o in self.outcomes)
                  for status in ("success", "failed", "truncated", "rate_limited", "cancelled")}
        state = "PARTIAL SEARCH" if self.exit_code else "Search complete"
        summary = state + ": " + ", ".join(f"{status}={count}" for status, count in counts.items()) + "".join(
            f"\n{o.site}{' category=' + o.category_id if o.category_id else ''} {o.query.keywords}: {o.status} {o.error}"
            for o in self.outcomes if o.status != "success")
        if self.recommendations:
            summary += "\n" + self.recommendations.summary
            if self.recommendations.rate_limited:
                summary += " (HTTP 429; unchecked matches retained)"
        return summary
