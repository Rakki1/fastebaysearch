"""Shared listing data for delivery and marketplace regression tests."""

from dataclasses import replace

from fastebaysearch_app.models import SearchResult


def item(**kwargs):
    value = SearchResult('camera','Camera','EBAY_GB','20 GBP','10001','https://www.ebay.co.uk/itm/10001','seller','','',
                         buying_options=('FIXED_PRICE',), fixed_amount='20',fixed_currency='GBP',source_site='EBAY_GB')
    return replace(value, **kwargs)
