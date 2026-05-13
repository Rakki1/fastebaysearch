import json
from pathlib import Path

import pytest

from fastebaysearch_app.config import ConfigError, load_config


def minimal_config():
    return {
        "use_email": False,
        "use_telegram": False,
        "ebay_sites": ["EBAY_US"],
        "exclude_terms": [],
        "ebay_client_id": "client-id",
        "ebay_client_secret": "client-secret",
        "ebay_urls_dbfile": "items.db",
        "ebay_oauth_file": "oauth_token.json",
        "ebay_search_keywords": {
            "base_terms": ["camera"],
            "required_terms": ["canon"],
        },
        "log_to_console": False,
    }


def test_load_config_validates_and_resolves_paths(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(minimal_config()), encoding="utf-8")

    config = load_config(config_path, tmp_path)

    assert config.db_path == tmp_path / "items.db"
    assert config.token_file == tmp_path / "oauth_token.json"
    assert config.ebay_client_id == "client-id"
    assert config.ebay_client_secret == "client-secret"
    assert config.email is None
    assert config.use_html_report is False
    assert config.html_report_dir == Path.cwd()
    assert config.html_report_max_per_run == 1000
    assert config.exchange_rate_cache_ttl_hours == 24
    assert config.log_max_size_mb == 10


def test_load_config_parses_log_max_size_mb(tmp_path):
    data = minimal_config()
    data["log_max_size_mb"] = "2"
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")

    config = load_config(config_path, tmp_path)

    assert config.log_max_size_mb == 2


def test_load_config_rejects_invalid_log_max_size_mb(tmp_path):
    data = minimal_config()
    data["log_max_size_mb"] = 0
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ConfigError, match="log_max_size_mb"):
        load_config(config_path, tmp_path)


def test_load_config_rejects_db_path_escape(tmp_path):
    data = minimal_config()
    data["ebay_urls_dbfile"] = "../outside.db"
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(config_path, tmp_path)


def test_load_config_rejects_invalid_integer_values(tmp_path):
    data = minimal_config()
    data["api_concurrency"] = "not-a-number"
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ConfigError, match="api_concurrency"):
        load_config(config_path, tmp_path)


def test_load_config_resolves_html_report_dir_from_current_working_directory(tmp_path, monkeypatch):
    data = minimal_config()
    data["use_html_report"] = True
    data["html_report_dir"] = "reports"
    data["html_report_max_per_run"] = "25"
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.chdir(run_dir)

    config = load_config(config_path, tmp_path)

    assert config.use_html_report is True
    assert config.html_report_dir == run_dir / "reports"
    assert config.html_report_max_per_run == 25


def test_load_config_rejects_absolute_html_report_dir(tmp_path):
    data = minimal_config()
    data["html_report_dir"] = str(tmp_path / "reports")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ConfigError, match="HTML report directory"):
        load_config(config_path, tmp_path)


def test_load_config_rejects_html_report_dir_escape(tmp_path, monkeypatch):
    data = minimal_config()
    data["html_report_dir"] = "../reports"
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.chdir(run_dir)

    with pytest.raises(ConfigError, match="HTML report directory"):
        load_config(config_path, tmp_path)


def test_load_config_rejects_invalid_html_report_max_per_run(tmp_path):
    data = minimal_config()
    data["html_report_max_per_run"] = 0
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ConfigError, match="html_report_max_per_run"):
        load_config(config_path, tmp_path)


def test_load_config_requires_ebay_credentials(tmp_path):
    data = minimal_config()
    del data["ebay_client_secret"]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ConfigError, match="ebay_client_secret"):
        load_config(config_path, tmp_path)


def test_load_config_rejects_non_object_json(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps([]), encoding="utf-8")

    with pytest.raises(ConfigError, match="Config root must be an object"):
        load_config(config_path, tmp_path)


def test_load_config_rejects_invalid_telegram_send_mode(tmp_path):
    data = minimal_config()
    data["use_telegram"] = True
    data["telegram_token"] = "token"
    data["telegram_chat_id"] = "chat"
    data["telegram_send_mode"] = "invalid"
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ConfigError, match="telegram_send_mode"):
        load_config(config_path, tmp_path)


def test_load_config_respects_smtp_authentication_flag(tmp_path):
    data = minimal_config()
    data.update(
        {
            "use_email": True,
            "smtp_server": "smtp.example.com",
            "smtp_port": "587",
            "smtp_login": "user",
            "smtp_password": "password",
            "smtp_authentication": "no",
            "email_sender": "from@example.com",
            "email_receiver": "to@example.com",
            "email_subject": "Subject",
            "email_person_name": "User",
        }
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")

    config = load_config(config_path, tmp_path)

    assert config.email is not None
    assert config.email.authenticate is False
