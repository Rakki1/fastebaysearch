from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from ..models import SearchResult
from .email import render_email_html


class HtmlReportNotifier:
    def __init__(
        self,
        report_dir: Path,
        max_per_run: int = 1000,
        logger: logging.Logger | None = None,
    ):
        self.report_dir = Path(report_dir)
        self.max_per_run = max(1, int(max_per_run))
        self.logger = logger or logging.getLogger("fastebaysearch")

    def send(self, results: list[SearchResult], person_name: str = "User") -> list[SearchResult]:
        if not results:
            return []

        selected_results = results[: self.max_per_run]
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
        report_path = self.report_dir / f"ebay_report_{timestamp}.html"

        try:
            self.report_dir.mkdir(parents=True, exist_ok=True)
            report_path.write_text(render_email_html(selected_results, person_name), encoding="utf-8")
        except OSError as exc:
            self.logger.error(f"HTML report write failed: {exc}")
            return []

        self.logger.info(f"HTML report written: {report_path}")
        return selected_results
