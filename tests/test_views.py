import pytest

from da_assistant.schema import DIRECT_PII, TABLES
from da_assistant.views import safe_views

DS = "bigquery-public-data.thelook_ecommerce"


@pytest.mark.parametrize("brands", [None, ("Calvin Klein", "Diesel")])
def test_views_have_exactly_the_allowed_columns(brands):
    views = safe_views(DS, brands)

    assert set(views) == set(TABLES)
    for name, view in views.items():
        assert view.named_selects == [column.name for column in TABLES[name].columns]
        assert not set(view.named_selects) & DIRECT_PII


def test_brand_views_filter_every_table():
    views = safe_views(DS, ("Calvin Klein",))

    for view in views.values():
        assert "brand IN ('Calvin Klein')" in view.sql(dialect="bigquery")


def test_views_for_all_brands_have_no_brand_filter():
    for view in safe_views(DS, None).values():
        assert "brand IN" not in view.sql(dialect="bigquery")
