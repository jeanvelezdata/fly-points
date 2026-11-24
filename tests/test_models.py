from datetime import date

import pytest
from pydantic import ValidationError

from flypoints.models import Bank, Cabin, SearchQuery

BASE = dict(origins=["SJU"], destinations=["MAD"], date_from=date(2026, 12, 1), date_to=date(2026, 12, 4))


def test_legacy_cabin_key_maps_to_cabins():
    q = SearchQuery(**BASE, cabin="J")
    assert q.cabins == [Cabin.J]
    assert "cabin" not in q.model_dump()
    assert SearchQuery.model_validate(q.model_dump()).cabins == [Cabin.J]  # round trip


def test_cabins_deduped_in_order_and_non_empty():
    assert SearchQuery(**BASE, cabins=["J", "Y", "J"]).cabins == [Cabin.J, Cabin.Y]
    with pytest.raises(ValidationError):
        SearchQuery(**BASE, cabins=[])
    with pytest.raises(ValidationError):
        SearchQuery(**BASE)


def test_cabins_wins_over_legacy_cabin():
    assert SearchQuery(**BASE, cabin="Y", cabins=["F"]).cabins == [Cabin.F]


def test_filter_defaults_and_validation():
    q = SearchQuery(**BASE, cabins=["J"])
    assert (q.direct_only, q.max_stops, q.carriers, q.banks) == (False, None, None, None)
    assert SearchQuery(**BASE, cabins=["J"], carriers=["ua", " av"]).carriers == ["UA", "AV"]
    assert SearchQuery(**BASE, cabins=["J"], banks=["chase"]).banks == [Bank.chase]
    with pytest.raises(ValidationError):
        SearchQuery(**BASE, cabins=["J"], days_of_week=[7])
