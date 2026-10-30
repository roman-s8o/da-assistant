"""The tables and columns the agent may use, with short notes for the model.

This is an allow list: the model sees only the columns listed here, and a new column
in BigQuery stays hidden until someone adds it. Direct personal data (names, email,
street address, postal code, coordinates) is left out on purpose, so the model
never learns that it exists.
"""

from dataclasses import dataclass

from da_assistant.warehouse import Warehouse


@dataclass(frozen=True)
class Column:
    name: str
    note: str
    # Personal attributes: the model may use them only in grouped results.
    aggregate_only: bool = False


@dataclass(frozen=True)
class Table:
    name: str
    note: str
    columns: tuple[Column, ...]


_STATUS = "'Processing', 'Shipped', 'Complete', 'Cancelled' or 'Returned'"

_TABLES = (
    Table(
        "orders",
        "One row per order.",
        (
            Column("order_id", "Order id."),
            Column("user_id", "Customer id (users.id)."),
            Column("status", f"Order status: {_STATUS}."),
            Column("gender", "Customer gender: 'M' or 'F'.", aggregate_only=True),
            Column("created_at", "When the order was placed."),
            Column("shipped_at", "When the order was shipped."),
            Column("delivered_at", "When the order was delivered."),
            Column("returned_at", "When the order was returned."),
            Column("num_of_item", "Number of items in the order."),
        ),
    ),
    Table(
        "order_items",
        "One row per item sold. The source of revenue.",
        (
            Column("id", "Order item id."),
            Column("order_id", "Order id (orders.order_id)."),
            Column("user_id", "Customer id (users.id)."),
            Column("product_id", "Product id (products.id)."),
            Column("inventory_item_id", "Inventory item id."),
            Column("status", f"Item status: {_STATUS}."),
            Column("created_at", "When the item was ordered."),
            Column("shipped_at", "When the item was shipped."),
            Column("delivered_at", "When the item was delivered."),
            Column("returned_at", "When the item was returned."),
            Column("sale_price", "Price the customer paid for the item, in USD."),
        ),
    ),
    Table(
        "products",
        "Product catalog.",
        (
            Column("id", "Product id."),
            Column("name", "Product name."),
            Column("brand", "Brand name."),
            Column("category", "Category, for example 'Jeans' or 'Swim'."),
            Column("department", "'Men' or 'Women'."),
            Column("cost", "Cost of one unit to the company, in USD."),
            Column("retail_price", "List price, in USD."),
            Column("sku", "Stock keeping unit."),
            Column("distribution_center_id", "Distribution center id."),
        ),
    ),
    Table(
        "users",
        "One row per customer.",
        (
            Column("id", "Customer id."),
            Column("age", "Age in years.", aggregate_only=True),
            Column("gender", "'M' or 'F'.", aggregate_only=True),
            Column("city", "City.", aggregate_only=True),
            Column("state", "State or region, for example 'California'.", aggregate_only=True),
            Column("country", "Country, for example 'United States'.", aggregate_only=True),
            Column(
                "traffic_source",
                "How the customer found the shop: 'Search', 'Organic', 'Facebook', "
                "'Email' or 'Display'.",
                aggregate_only=True,
            ),
            Column("created_at", "When the customer signed up."),
        ),
    ),
)

TABLES: dict[str, Table] = {table.name: table for table in _TABLES}

# Direct personal data in BigQuery. These columns are never shown, never sent to the
# model and cannot be used in a query, not even in a filter.
DIRECT_PII = frozenset(
    {
        "first_name",
        "last_name",
        "email",
        "street_address",
        "postal_code",
        "latitude",
        "longitude",
        "user_geom",
    }
)

# Columns about people. They can be used only in grouped results.
PEOPLE_COLUMNS = frozenset(
    column.name for table in _TABLES for column in table.columns if column.aggregate_only
)

# The customer id column of each table that has one.
USER_KEYS = {"users": "id", "orders": "user_id", "order_items": "user_id"}

JOINS = (
    "order_items.order_id = orders.order_id",
    "order_items.product_id = products.id",
    "order_items.user_id = users.id",
    "orders.user_id = users.id",
)


def column_types(warehouse: Warehouse) -> dict[str, dict[str, str]]:
    """The live column types of each table, read with the client's `get_table_schema`."""
    return {
        name: {field["name"]: field["type"] for field in warehouse.runner.get_table_schema(name)}
        for name in TABLES
    }


def schema_text(dataset: str, types: dict[str, dict[str, str]]) -> str:
    """The schema as text for the system prompt.

    Only allowed columns that exist in BigQuery are listed, with their live types.
    """
    parts = []
    for table in TABLES.values():
        live = types.get(table.name, {})
        lines = [f"Table `{dataset}.{table.name}`: {table.note}"]
        for column in table.columns:
            if column.name not in live:
                continue
            note = column.note + (" Use only in grouped results." if column.aggregate_only else "")
            lines.append(f"- {column.name} ({live[column.name]}): {note}")
        parts.append("\n".join(lines))
    parts.append("Joins:\n" + "\n".join(f"- {join}" for join in JOINS))
    return "\n\n".join(parts)
