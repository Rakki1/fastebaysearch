from __future__ import annotations

import logging
import os
import tempfile
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
        self.last_report_path: Path | None = None

    def send(self, results: list[SearchResult], person_name: str = "User",
             run_summary: str = "") -> list[SearchResult]:
        self.last_report_path = None
        if not results:
            return []

        selected_results = results[: self.max_per_run]
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
        report_path = self.report_dir / f"ebay_report_{timestamp}.html"
        tmp_path = None

        try:
            self.report_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.report_dir,
                                             prefix=".ebay_report_", suffix=".tmp", delete=False) as handle:
                tmp_path = Path(handle.name)
                handle.write(render_email_html(selected_results, person_name, run_summary))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, report_path)
        except OSError as exc:
            self.logger.error(f"HTML report write failed: {exc}")
            return []
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    self.logger.warning("Could not remove temporary HTML report: %s", tmp_path)

        self.last_report_path = report_path
        self.logger.info(f"HTML report written: {report_path}")
        return selected_results
