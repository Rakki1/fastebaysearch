from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import normalize_terms


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SearchConfig:
    base_terms: list[str]
    required_terms: list[str]


@dataclass(frozen=True)
class EmailConfig:
    smtp_server: str
    smtp_port: int
    smtp_login: str
    smtp_password: str
    sender: str
    receiver: str
    subject: str
    person_name: str
    starttls: bool = True
    authenticate: bool = True
    max_per_run: int = 1000


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_id: str
    max_per_run: int = 1000
    send_mode: str = "auto"
    disable_web_preview: bool = False


@dataclass(frozen=True)
class AppConfig:
    config_path: Path
    script_dir: Path
    db_path: Path
    token_file: Path
    ebay_client_id: str
    ebay_client_secret: str
    use_email: bool
    use_telegram: bool
    ebay_sites: list[str]
    exclude_terms: list[str]
    search: SearchConfig
    email: EmailConfig | None
    telegram: TelegramConfig | None
    log_to_console: bool
    timezone: str
    raw: dict[str, Any]
    api_concurrency: int = 8
    exchange_rate_cache_ttl_hours: int = 24
    ebay_daily_api_limit: int = 10000
    api_budget_safety_percent: int = 90
    estimated_results_per_query: int = 200


def as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def require_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None or str(value).strip() == "":
        raise ConfigError(f"Missing required config value: {key}")
    return str(value)


def parse_int(value: object, key: str, default: int, minimum: int | None = None) -> int:
    if value is None:
        parsed = default
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{key} must be an integer") from exc

    if minimum is not None and parsed < minimum:
        raise ConfigError(f"{key} must be at least {minimum}")
    return parsed


def parse_percent(value: object, key: str, default: int) -> int:
    parsed = parse_int(value, key, default, minimum=1)
    if parsed > 100:
        raise ConfigError(f"{key} must be at most 100")
    return parsed


def resolve_under_script_dir(script_dir: Path, configured_path: object, default_name: str, label: str) -> Path:
    raw = str(configured_path or default_name)
    expanded = os.path.expanduser(raw)
    candidate = Path(expanded) if os.path.isabs(expanded) else script_dir / expanded
    resolved = candidate.resolve()
    script_root = script_dir.resolve()

    try:
        resolved.relative_to(script_root)
    except ValueError as exc:
        raise ConfigError(f"{label} must stay under script directory: {raw}") from exc

    return resolved


def load_config(config_path: Path, script_dir: Path) -> AppConfig:
    config_path = config_path.resolve()
    script_dir = script_dir.resolve()

    with config_path.open("r", encoding="utf-8") as handle:
        raw: dict[str, Any] = json.load(handle)

    search_raw = raw.get("ebay_search_keywords")
    if not isinstance(search_raw, dict):
        raise ConfigError("ebay_search_keywords must be an object")

    search = SearchConfig(
        base_terms=normalize_terms(search_raw.get("base_terms")),
        required_terms=normalize_terms(search_raw.get("required_terms")),
    )
    if not search.base_terms:
        raise ConfigError("ebay_search_keywords.base_terms must contain at least one value")

    ebay_sites = normalize_terms(raw.get("ebay_sites"))
    if not ebay_sites:
        raise ConfigError("ebay_sites must contain at least one marketplace")

    use_email = as_bool(raw.get("use_email"), True)
    email = None
    if use_email:
        email = EmailConfig(
            smtp_server=require_text(raw, "smtp_server"),
            smtp_port=parse_int(raw.get("smtp_port"), "smtp_port", 587, minimum=1),
            smtp_login=require_text(raw, "smtp_login"),
            smtp_password=require_text(raw, "smtp_password"),
            sender=require_text(raw, "email_sender"),
            receiver=require_text(raw, "email_receiver"),
            subject=require_text(raw, "email_subject"),
            person_name=require_text(raw, "email_person_name"),
            starttls=as_bool(raw.get("smtp_starttls_encryption"), True),
            authenticate=as_bool(raw.get("smtp_authentication"), True),
            max_per_run=parse_int(raw.get("email_max_per_run"), "email_max_per_run", 1000, minimum=1),
        )

    use_telegram = as_bool(raw.get("use_telegram"), False)
    telegram = None
    if use_telegram:
        send_mode = str(raw.get("telegram_send_mode") or "auto").strip().lower()
        if send_mode not in {"auto", "photo", "photo_only", "text"}:
            raise ConfigError("telegram_send_mode must be one of: auto, photo, photo_only, text")
        telegram = TelegramConfig(
            token=require_text(raw, "telegram_token"),
            chat_id=require_text(raw, "telegram_chat_id"),
            max_per_run=parse_int(raw.get("telegram_max_per_run"), "telegram_max_per_run", 1000, minimum=1),
            send_mode=send_mode,
            disable_web_preview=as_bool(raw.get("telegram_disable_web_preview"), False),
        )

    return AppConfig(
        config_path=config_path,
        script_dir=script_dir,
        db_path=resolve_under_script_dir(script_dir, raw.get("ebay_urls_dbfile"), "ebay_items.db", "Database path"),
        token_file=resolve_under_script_dir(script_dir, raw.get("ebay_oauth_file"), "oauth_token.json", "Token cache path"),
        ebay_client_id=require_text(raw, "ebay_client_id"),
        ebay_client_secret=require_text(raw, "ebay_client_secret"),
        use_email=use_email,
        use_telegram=use_telegram,
        ebay_sites=ebay_sites,
        exclude_terms=normalize_terms(raw.get("exclude_terms")),
        search=search,
        email=email,
        telegram=telegram,
        log_to_console=as_bool(raw.get("log_to_console"), True),
        timezone=str(raw.get("timezone") or "UTC"),
        raw=raw,
        api_concurrency=parse_int(raw.get("api_concurrency"), "api_concurrency", 8, minimum=1),
        exchange_rate_cache_ttl_hours=parse_int(
            raw.get("exchange_rate_cache_ttl_hours"),
            "exchange_rate_cache_ttl_hours",
            24,
            minimum=0,
        ),
        ebay_daily_api_limit=parse_int(raw.get("ebay_daily_api_limit"), "ebay_daily_api_limit", 10000, minimum=1),
        api_budget_safety_percent=parse_percent(raw.get("api_budget_safety_percent"), "api_budget_safety_percent", 90),
        estimated_results_per_query=parse_int(
            raw.get("estimated_results_per_query"),
            "estimated_results_per_query",
            200,
            minimum=0,
        ),
    )
