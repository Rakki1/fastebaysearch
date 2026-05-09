from fastebaysearch_app.ebay_client import parse_search_results


def test_parse_search_results_handles_nullable_fields():
    raw_items = [
        {
            "itemId": "v1|1234567890|0",
            "price": None,
            "title": None,
            "listingMarketplaceId": None,
            "itemWebUrl": None,
            "seller": None,
            "image": {"imageUrl": "javascript:alert(1)"},
        },
        None,
    ]

    results = parse_search_results("camera", raw_items, {})

    assert len(results) == 1
    assert results[0].item_id == "1234567890"
    assert results[0].name == "Unknown"
    assert results[0].price == "0.00 EUR"
    assert results[0].link == ""
    assert results[0].image == ""


def test_parse_search_results_converts_known_currency():
    raw_items = [
        {
            "legacyItemId": "55555",
            "price": {"value": "12", "currency": "USD"},
            "title": "Lens",
            "listingMarketplaceId": "EBAY_US",
            "itemWebUrl": "https://example.com/item?hash=1",
            "seller": {"username": "seller"},
        }
    ]

    results = parse_search_results("lens", raw_items, {"USD": 1.2})

    assert results[0].price == "10.00 EUR (12.00 USD)"
    assert results[0].link == "https://example.com/item"
