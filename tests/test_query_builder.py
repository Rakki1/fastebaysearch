from pathlib import Path

import pytest

from fastebaysearch_app.config import AppConfig, ConfigError, SearchConfig
from fastebaysearch_app.query_builder import build_queries


def make_config():
    return AppConfig(
        db_path=None,
        token_file=None,
        ebay_client_id="client-id",
        ebay_client_secret="client-secret",
        use_email=False,
        use_telegram=False,
        use_html_report=False,
        html_report_dir=Path("."),
        html_report_max_per_run=1000,
        ebay_sites=["EBAY_US"],
        exclude_terms=["broken", "too-long-exclude-term"],
        search=SearchConfig(base_terms=["camera"], required_terms=["canon", "nikon"]),
        email=None,
        telegram=None,
        log_to_console=False,
    )


def test_build_queries_preserves_config_shape():
    config = make_config()

    queries = build_queries(config)

    assert len(queries) == 1
    assert queries[0].base_name == "camera"
    assert queries[0].keywords == '("canon", "nikon") camera -"broken" -"too-long-exclude-term"'
    assert isinstance(config.search, SearchConfig)


@pytest.mark.parametrize("length", [99, 100, 101])
def test_query_length_boundary(length):
    config = make_config()
    config.search.base_terms[:] = ["x" * length]
    config.search.required_terms.clear()
    config.exclude_terms.clear()
    if length <= 100:
        assert len(build_queries(config)[0].keywords) == length
    else:
        with pytest.raises(ConfigError, match="101 characters"):
            build_queries(config)


def test_long_exclusion_does_not_silently_drop_later_short_exclusion():
    config = make_config()
    config.exclude_terms[:] = ["x" * 100, "broken"]
    with pytest.raises(ConfigError) as error:
        build_queries(config)
    assert '-"broken"' in str(error.value)


def test_collector_searches_preserve_all_expressions(tmp_path):
    from fastebaysearch_app.config import load_config
    config = load_config(Path(__file__).parent / "fixtures" / "collector_search.json", tmp_path)
    queries = build_queries(config)
    assert len(queries) == 13
    assert len(config.ebay_sites) * len(queries) == 78
    assert all(q.keywords == '("commodore", "c64", "c-64") ' + q.base_name + ' -"Keilriemen"' for q in queries)
    assert min(len(q.keywords) for q in queries) == 48
    assert max(len(q.keywords) for q in queries) == 66
    assert queries[9].base_name == '(Slapshot, "Slap Shot")'
    assert queries[11].base_name == 'Kong -hong'
