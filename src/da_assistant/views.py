"""Safe views: the part of each table a user can see.

Before a query runs, every table in it is replaced with one of these views:

- No direct personal data. A view has only the allowed columns from `schema.py`,
  so no query can read a name, an email or an address, whatever it looks like.
- Brand access. For a brand manager, `products` holds only their brands, and
  `order_items`, `orders` and `users` only the rows linked to those products.
  `orders.num_of_item` counts in-scope items only, because an order can mix brands.

In production, the same views would be BigQuery authorized views or row-level
access policies, so the scope would not depend on rewriting queries.
"""

from functools import lru_cache

import sqlglot
from sqlglot import exp

from da_assistant.schema import TABLES


@lru_cache(maxsize=32)
def safe_views(dataset: str, brands: tuple[str, ...] | None) -> dict[str, exp.Query]:
    """The view for each table. `brands=None` means all brands."""
    products = f"`{dataset}.products`"
    if brands is None:
        sql = {name: f"SELECT {_columns(name)} FROM `{dataset}.{name}`" for name in TABLES}
    else:
        in_scope = f"brand IN ({', '.join(_string(brand) for brand in brands)})"
        product_ids = f"SELECT id FROM {products} WHERE {in_scope}"
        items = f"`{dataset}.order_items`"
        item_counts = (
            f"SELECT order_id, COUNT(*) AS num_of_item FROM {items} "
            f"WHERE product_id IN ({product_ids}) GROUP BY order_id"
        )
        sql = {
            "products": f"SELECT {_columns('products')} FROM {products} WHERE {in_scope}",
            "order_items": (
                f"SELECT {_columns('order_items')} FROM {items} WHERE product_id IN ({product_ids})"
            ),
            "orders": (
                f"SELECT {_columns('orders', table_alias='o', num_of_item='i.num_of_item')} "
                f"FROM `{dataset}.orders` AS o "
                f"JOIN ({item_counts}) AS i ON i.order_id = o.order_id"
            ),
            "users": (
                f"SELECT {_columns('users')} FROM `{dataset}.users` WHERE id IN "
                f"(SELECT user_id FROM {items} WHERE product_id IN ({product_ids}))"
            ),
        }
    return {name: sqlglot.parse_one(text, read="bigquery") for name, text in sql.items()}


def apply_views(query: exp.Expression, dataset: str, brands: tuple[str, ...] | None) -> None:
    """Replace every table of the dataset in `query` with its safe view, in place."""
    views = safe_views(dataset, brands)
    for table in list(query.find_all(exp.Table)):
        if f"{table.catalog}.{table.db}" == dataset and table.name in views:
            # The view keeps the table's alias (or its name), so column references still work.
            table.replace(views[table.name].copy().subquery(table.alias_or_name))


def _columns(table: str, *, table_alias: str = "", **replacements: str) -> str:
    """The allowed columns of a table, as a SELECT list."""
    names = []
    for column in TABLES[table].columns:
        if column.name in replacements:
            names.append(f"{replacements[column.name]} AS {column.name}")
        else:
            names.append(f"{table_alias}.{column.name}" if table_alias else column.name)
    return ", ".join(names)


def _string(value: str) -> str:
    """A safely quoted SQL string literal."""
    return exp.Literal.string(value).sql(dialect="bigquery")
