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
