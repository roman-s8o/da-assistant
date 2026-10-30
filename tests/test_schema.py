from da_assistant.schema import TABLES, schema_text

# Direct personal data in `users`. It must never be shown to the model.
DIRECT_PII = {
    "first_name",
    "last_name",
    "email",
    "street_address",
    "postal_code",
    "latitude",
    "longitude",
    "user_geom",
}


def live_types(**extra: dict[str, str]) -> dict[str, dict[str, str]]:
    """Types as BigQuery would return them: every allowed column, plus `extra`."""
    types = {name: {c.name: "STRING" for c in table.columns} for name, table in TABLES.items()}
    for table, columns in extra.items():
        types[table].update(columns)
    return types


def test_allow_list_has_no_direct_pii():
    allowed = {column.name for column in TABLES["users"].columns}

    assert not allowed & DIRECT_PII


def test_schema_text_hides_columns_that_are_not_allowed():
    # BigQuery has the PII columns, but the model must not see them.
    text = schema_text("p.d", live_types(users={name: "STRING" for name in DIRECT_PII}))

    for name in DIRECT_PII:
        assert name not in text


def test_schema_text_skips_allowed_columns_missing_in_bigquery():
    types = live_types()
    del types["orders"]["num_of_item"]

    assert "num_of_item" not in schema_text("p.d", types)


def test_schema_text_lists_tables_types_and_rules():
    text = schema_text("p.d", live_types(order_items={"sale_price": "FLOAT"}))

    assert "Table `p.d.order_items`" in text
    assert "- sale_price (FLOAT): " in text
    assert "order_items.product_id = products.id" in text
    state_line = next(line for line in text.splitlines() if line.startswith("- state "))
    assert "Use only in grouped results." in state_line
