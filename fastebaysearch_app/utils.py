from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


def normalize_terms(values: object) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        values = [values]
    return [str(value).strip() for value in values if value is not None and str(value).strip()]


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def scalar_text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def clean_ebay_id(item_id: object) -> str | None:
    if not item_id:
        return None

    value = str(item_id)
    match = re.search(r"\|(\d+)\|", value)
    if match:
        return match.group(1)

    if re.fullmatch(r"\d+", value):
        return value

    match = re.search(r"\d{5,}", value)
    return match.group(0) if match else None


def safe_url(url: object) -> str:
    if not url:
        return ""
    value = str(url).strip()
    parsed = urlparse(value)
    if parsed.scheme.lower() in ("http", "https"):
        return value
    return ""


def format_date(date_str: object, logger=None) -> str:
    if not date_str or date_str == "Not available":
        return "Not available"

    value = str(date_str).strip()
    if not value:
        return "Not available"

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M:%S")
    except ValueError:
        pass

    try:
        from dateutil import parser
    except ImportError as exc:
        if logger:
            logger.debug(f"dateutil parser unavailable for date value {date_str!r}: {exc}")
        return "Not available"

    try:
        dt = parser.parse(value)
    except (TypeError, ValueError, OverflowError) as exc:
        if logger:
            logger.debug(f"format_date parse failed: {date_str!r}: {exc}")
        return "Not available"

    return dt.strftime("%d.%m.%Y %H:%M:%S")


def write_private_json_atomic(path: Path, data: dict[str, object]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: str | None = None

    try:
        fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass

        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise
