import json
import os

from fastebaysearch_app.utils import safe_url, write_private_json_atomic


def test_safe_url_allows_only_http_https():
    assert safe_url("https://example.com") == "https://example.com"
    assert safe_url("http://example.com") == "http://example.com"
    assert safe_url("javascript:alert(1)") == ""
    assert safe_url(None) == ""


def test_write_private_json_atomic(tmp_path):
    path = tmp_path / "token.json"

    write_private_json_atomic(path, {"access_token": "abc"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"access_token": "abc"}
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
