import logging
from logging.handlers import RotatingFileHandler

import pytest

from fastebaysearch_app.logging_setup import setup_logging


@pytest.fixture(autouse=True)
def clean_fastebaysearch_logger():
    logger = logging.getLogger("fastebaysearch")
    yield
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


class CloseTrackingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.closed_by_setup = False

    def emit(self, record):
        pass

    def close(self):
        self.closed_by_setup = True
        super().close()


def test_setup_logging_closes_existing_handlers(tmp_path):
    logger = logging.getLogger("fastebaysearch")
    previous_handler = CloseTrackingHandler()
    logger.addHandler(previous_handler)

    setup_logging(tmp_path / "fastebaysearch.log", log_to_console=False, log_max_size_mb=2)

    assert previous_handler.closed_by_setup is True
    assert previous_handler not in logger.handlers


def test_setup_logging_uses_rotating_file_handler(tmp_path):
    logger = setup_logging(tmp_path / "fastebaysearch.log", log_to_console=False, log_max_size_mb=2)

    assert len(logger.handlers) == 1
    handler = logger.handlers[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == 2 * 1024 * 1024
    assert handler.backupCount == 5


def test_setup_logging_adds_console_handler_when_enabled(tmp_path):
    logger = setup_logging(tmp_path / "fastebaysearch.log", log_to_console=True, log_max_size_mb=1)

    assert len(logger.handlers) == 2
    assert any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers)
    assert any(type(handler) is logging.StreamHandler for handler in logger.handlers)


@pytest.mark.parametrize("level", [None, "DEBUG"])
def test_log_detail_can_be_reduced_without_losing_search_failures(tmp_path, monkeypatch, level):
    import asyncio
    import time
    import aiohttp

    from fastebaysearch_app.cli import _log_queries
    from fastebaysearch_app.ebay_client import EbayClient
    from fastebaysearch_app.models import SearchQuery
    from fastebaysearch_app.workflow import WorkflowResult, log_summary
    from http_fakes import Response, Session

    path = tmp_path / "fastebaysearch.log"
    options = {} if level is None else {"log_level": level}
    logger = setup_logging(path, False, 1, **options)
    query = SearchQuery("Camera", "Camera", positive_groups=(("camera",),))
    _log_queries(logger, [query])

    def respond(call):
        if call["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US":
            return Response(data={"total": 50, "warnings": [{"errorId": 12009}]})
        return Response(data={"total": 2, "itemSummaries": [
            {"itemId": "v1|12345|0", "title": "Camera"},
            {"itemId": "v1|12346|0", "title": "Lens"},
        ]})

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: Session(respond))
    client = EbayClient("fake", logger=logger, category_ids={"EBAY_US": ["1249"]},
                        validate_search_terms=True, search_in_description=False)
    run = asyncio.run(client.run_queries(["EBAY_GB", "EBAY_US"], [query], {}))
    log_summary(logger, WorkflowResult(run.items, run.items, [], run), time.time())
    text = path.read_text(encoding="utf-8")

    assert "Search results: EBAY_GB successful=1 incomplete=0" in text
    assert "Search results: EBAY_US successful=0 incomplete=1" in text
    assert "Search failed: EBAY_US category=1249 query='Camera'" in text
    assert "reason=Invalid category_ids (eBay warning 12009)" in text
    assert "Browse API calls, including descriptions and retries: 2" in text
    assert "Search term check: accepted=1, rejected=1, deferred=0, description_calls=0" in text
    assert "Finished with exit_code=1. Raw results: 2 | Items after checks: 1" in text
    assert ("Search success: EBAY_GB" in text) is (level == "DEBUG")
    assert ("Base: Camera -> Camera" in text) is (level == "DEBUG")
    assert ("Search term check rejected:" in text) is (level == "DEBUG")
