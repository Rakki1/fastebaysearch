from fastebaysearch_app.config import AppConfig, SearchConfig
from fastebaysearch_app.query_builder import build_queries


def make_config():
    return AppConfig(
        db_path=None,
        token_file=None,
        ebay_client_id="client-id",
        ebay_client_secret="client-secret",
        use_email=False,
        use_telegram=False,
        ebay_sites=["EBAY_US"],
        exclude_terms=["broken", "too-long-exclude-term"],
        search=SearchConfig(base_terms=["camera"], required_terms=["canon", "nikon"]),
        email=None,
        telegram=None,
        log_to_console=False,
    )


def test_build_queries_preserves_config_shape():
    config = make_config()

    queries = build_queries(config, max_q_len=40)

    assert len(queries) == 1
    assert queries[0].base_name == "camera"
    assert queries[0].keywords == '("canon", "nikon") camera -"broken"'
    assert isinstance(config.search, SearchConfig)
