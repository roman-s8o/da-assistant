"""Privacy and brand access, tested on real data.

The checked SQL is translated to DuckDB and run on a small copy of the four tables,
including the personal data columns, filled with known values. So these tests check
what a user would actually get back, not only how the SQL looks.
"""

import duckdb
import pytest
import sqlglot

from da_assistant.schema import DIRECT_PII
from da_assistant.sql_checks import SqlRejected, check_sql

DS = "bigquery-public-data.thelook_ecommerce"
USERS, ORDERS, ITEMS, PRODUCTS = (
    f"`{DS}.{t}`" for t in ("users", "orders", "order_items", "products")
)
ALICE = ("Calvin Klein", "Diesel")

# Products: 1 Calvin Klein, 2 Diesel, 3 Nike, 4 Levi's.
# Users 1-6 live in California and buy Calvin Klein (Diesel for user 6).
# User 7 (Texas) buys only Nike. User 8 (Nevada) buys Calvin Klein and Nike in one
# order. User 9 (Oregon) has no orders.
_DATA = """
CREATE TABLE {t}.products (id INT, name VARCHAR, brand VARCHAR, category VARCHAR,
    department VARCHAR, cost DOUBLE, retail_price DOUBLE, sku VARCHAR,
    distribution_center_id INT);
INSERT INTO {t}.products VALUES
    (1, 'CK Jeans', 'Calvin Klein', 'Jeans', 'Women', 20, 50, 's1', 1),
    (2, 'Diesel Tee', 'Diesel', 'Tops', 'Men', 10, 30, 's2', 1),
    (3, 'Nike Shoe', 'Nike', 'Active', 'Men', 30, 90, 's3', 1),
    (4, 'Levi''s 501', 'Levi''s', 'Jeans', 'Men', 25, 60, 's4', 1);

CREATE TABLE {t}.users (id INT, first_name VARCHAR, last_name VARCHAR, email VARCHAR,
    age INT, gender VARCHAR, state VARCHAR, street_address VARCHAR, postal_code VARCHAR,
    city VARCHAR, country VARCHAR, latitude DOUBLE, longitude DOUBLE,
    traffic_source VARCHAR, created_at TIMESTAMP, user_geom VARCHAR);
INSERT INTO {t}.users
SELECT i, 'First' || i, 'Last' || i, 'user' || i || '@example.com', 20 + i,
    CASE WHEN i IN (6, 7, 9) THEN 'M' ELSE 'F' END,
    CASE WHEN i <= 6 THEN 'California' WHEN i = 7 THEN 'Texas'
         WHEN i = 8 THEN 'Nevada' ELSE 'Oregon' END,
    i || ' Secret Street', 'ZIP' || i,
    CASE WHEN i <= 6 THEN 'Los Angeles' ELSE 'Town' || i END,
    'United States', 34.0 + i, -118.0 - i, 'Search', TIMESTAMP '2026-01-01',
    'POINT(' || i || ' ' || i || ')'
FROM range(1, 10) AS r(i);

CREATE TABLE {t}.orders (order_id INT, user_id INT, status VARCHAR, gender VARCHAR,
    created_at TIMESTAMP, returned_at TIMESTAMP, shipped_at TIMESTAMP,
    delivered_at TIMESTAMP, num_of_item INT);
INSERT INTO {t}.orders
SELECT 100 + id, id, 'Complete', gender, TIMESTAMP '2026-09-01', NULL, NULL, NULL,
    CASE WHEN id = 8 THEN 2 ELSE 1 END
FROM {t}.users WHERE id <= 8;

CREATE TABLE {t}.order_items (id INT, order_id INT, user_id INT, product_id INT,
    inventory_item_id INT, status VARCHAR, created_at TIMESTAMP, shipped_at TIMESTAMP,
    delivered_at TIMESTAMP, returned_at TIMESTAMP, sale_price DOUBLE);
INSERT INTO {t}.order_items VALUES
    (1, 101, 1, 1, 1, 'Complete', TIMESTAMP '2026-09-01', NULL, NULL, NULL, 50),
    (2, 102, 2, 1, 2, 'Complete', TIMESTAMP '2026-09-01', NULL, NULL, NULL, 50),
    (3, 103, 3, 1, 3, 'Complete', TIMESTAMP '2026-09-01', NULL, NULL, NULL, 50),
    (4, 104, 4, 1, 4, 'Complete', TIMESTAMP '2026-09-01', NULL, NULL, NULL, 50),
    (5, 105, 5, 1, 5, 'Complete', TIMESTAMP '2026-09-01', NULL, NULL, NULL, 50),
    (6, 106, 6, 2, 6, 'Complete', TIMESTAMP '2026-09-01', NULL, NULL, NULL, 30),
    (7, 107, 7, 3, 7, 'Complete', TIMESTAMP '2026-09-01', NULL, NULL, NULL, 90),
    (8, 108, 8, 1, 8, 'Complete', TIMESTAMP '2026-09-01', NULL, NULL, NULL, 50),
    (9, 108, 8, 3, 9, 'Complete', TIMESTAMP '2026-09-01', NULL, NULL, NULL, 90);
"""

# Values that would show that personal data got out.
_PII_MARKERS = ("First", "Last", "@example.com", "Secret Street", "ZIP", "POINT(")


@pytest.fixture(scope="module")
def db():
    connection = duckdb.connect()
    connection.execute("ATTACH ':memory:' AS \"bigquery-public-data\"")
    connection.execute('CREATE SCHEMA "bigquery-public-data".thelook_ecommerce')
    connection.execute(_DATA.format(t='"bigquery-public-data".thelook_ecommerce'))
    yield connection
    connection.close()


def run(db, sql, brands=None):
    """Check the query for a user with these brands (None: all brands) and run it."""
    checked = check_sql(sql, dataset=DS, brands=brands, max_rows=1000)
    duckdb_sql = sqlglot.transpile(checked.sql, read="bigquery", write="duckdb")[0]
    rows = db.execute(duckdb_sql).fetchall()
    for row in rows:
        for value in row:
            assert not any(marker in str(value) for marker in _PII_MARKERS), row
    return rows


def rejected(sql, brands=None):
    with pytest.raises(SqlRejected) as error:
        check_sql(sql, dataset=DS, brands=brands, max_rows=1000)
    return str(error.value)


# Direct personal data ------------------------------------------------------------

_PII_SHAPES = {
    "plain": "SELECT {c} FROM {users}",
    "alias": "SELECT u.{c} AS x FROM {users} AS u",
    "upper case": "SELECT {upper} FROM {users}",
    "function": "SELECT LOWER(CAST({c} AS STRING)) FROM {users}",
    "filter only": "SELECT COUNT(*) FROM {users} WHERE CAST({c} AS STRING) LIKE '%a%'",
    "subquery": "SELECT x FROM (SELECT {c} AS x FROM {users}) AS t",
    "cte": "WITH c AS (SELECT {c} AS x FROM {users}) SELECT COUNT(x) FROM c",
    "union": (
        "SELECT CAST(id AS STRING) FROM {users} UNION ALL SELECT CAST({c} AS STRING) FROM {users}"
    ),
    "join": (
        f"SELECT o.order_id FROM {ORDERS} o JOIN {{users}} u ON CAST(u.{{c}} AS STRING) = o.status"
    ),
}


@pytest.mark.parametrize("column", sorted(DIRECT_PII))
@pytest.mark.parametrize("shape", _PII_SHAPES)
def test_direct_pii_is_rejected_in_any_shape(shape, column):
    sql = _PII_SHAPES[shape].format(c=column, upper=column.upper(), users=USERS)

    assert "personal data" in rejected(sql)


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT * FROM {USERS}",
        f"SELECT u.* FROM {USERS} AS u",
        f"SELECT * EXCEPT (age) FROM {USERS}",
        f"SELECT x FROM (SELECT * FROM {USERS}) AS t",
    ],
)
def test_star_is_rejected(sql):
    assert "List the columns" in rejected(sql)


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT TO_JSON_STRING(u) FROM {USERS} AS u",
        f"SELECT u FROM {USERS} AS u",
        f"SELECT COUNT(DISTINCT TO_JSON_STRING(users)) FROM {USERS}",
    ],
)
def test_whole_rows_are_rejected(sql):
    assert "whole table row" in rejected(sql)


def test_views_never_hold_personal_data(db):
    # Even a query that passes every rule reads only the safe columns.
    checked = check_sql(
        f"SELECT u.id, u.created_at FROM {USERS} AS u", dataset=DS, brands=None, max_rows=10
    )

    for column in DIRECT_PII:
        assert column not in checked.sql
    assert len(run(db, f"SELECT u.id, u.created_at FROM {USERS} AS u")) == 9


def test_top_customers_show_ids_and_spend_only(db):
    sql = (
        f"SELECT oi.user_id, SUM(oi.sale_price) AS spend FROM {ITEMS} AS oi "
        "GROUP BY oi.user_id ORDER BY spend DESC, oi.user_id LIMIT 3"
    )

    assert run(db, sql) == [(8, 140.0), (7, 90.0), (1, 50.0)]


# Brand access ----------------------------------------------------------------------

_SCOPE_SHAPES = {
    "plain": f"SELECT brand FROM {PRODUCTS}",
    "alias": f"SELECT x.brand FROM {PRODUCTS} AS x",
    "no alias": f"SELECT products.brand FROM {PRODUCTS}",
    "subquery": f"SELECT b AS brand FROM (SELECT brand AS b FROM {PRODUCTS}) AS t",
    "cte": f"WITH p AS (SELECT id, brand FROM {PRODUCTS}) SELECT brand FROM p",
    "union": f"SELECT brand FROM {PRODUCTS} UNION ALL SELECT name FROM {PRODUCTS}",
    "join": (f"SELECT p.brand FROM {ITEMS} AS oi JOIN {PRODUCTS} AS p ON p.id = oi.product_id"),
}


@pytest.mark.parametrize("shape", _SCOPE_SHAPES)
def test_brand_manager_sees_only_their_brands(db, shape):
    values = {row[0] for row in run(db, _SCOPE_SHAPES[shape], brands=ALICE)}

    assert values <= {"Calvin Klein", "Diesel", "CK Jeans", "Diesel Tee"}
    assert "Calvin Klein" in values


@pytest.mark.parametrize(
    ("sql", "alice", "ceo"),
    [
        # Revenue counts only in-scope items, also in an order that mixes brands.
        (f"SELECT SUM(sale_price) FROM {ITEMS}", 330.0, 510.0),
        (f"SELECT SUM(sale_price) FROM {ITEMS} WHERE product_id = 3", None, 180.0),
        (f"SELECT COUNT(*) FROM {PRODUCTS} WHERE LOWER(brand) = 'nike'", 0, 1),
        (
            f"SELECT COUNT(*) FROM {ITEMS} WHERE product_id IN (SELECT id FROM {PRODUCTS})",
            7,
            9,
        ),
        # Customers are visible only through purchases of in-scope products.
        (f"SELECT COUNT(id) FROM {USERS}", 7, 9),
        (f"SELECT COUNT(order_id) FROM {ORDERS}", 7, 8),
        # Order 108 has a Calvin Klein item and a Nike item: Alice sees one item.
        (f"SELECT num_of_item FROM {ORDERS} WHERE order_id = 108", 1, 2),
    ],
)
def test_brand_scope_holds_for_every_table(db, sql, alice, ceo):
    assert run(db, sql, brands=ALICE) == [(alice,)]
    assert run(db, sql) == [(ceo,)]


@pytest.mark.parametrize(
    "condition",
    [
        "brand = 'Nike'",
        "'Nike' = brand",
        "p.brand IN ('Diesel', 'Nike')",
        "brand = 'nike'",
    ],
)
def test_asking_for_another_brand_is_rejected_with_a_clear_reason(condition):
    message = rejected(f"SELECT COUNT(*) FROM {PRODUCTS} AS p WHERE {condition}", brands=ALICE)

    assert "only these brands: Calvin Klein, Diesel" in message
    assert "Nike" in message or "nike" in message


def test_in_scope_brand_filter_is_fine_in_any_case(db):
    assert run(db, f"SELECT COUNT(*) FROM {PRODUCTS} WHERE brand = 'diesel'", brands=ALICE) == [
        (0,)
    ]
    assert run(db, f"SELECT COUNT(*) FROM {PRODUCTS} WHERE brand = 'Diesel'", brands=ALICE) == [
        (1,)
    ]


def test_brand_names_are_quoted_safely(db):
    assert run(db, f"SELECT name FROM {PRODUCTS}", brands=("Levi's",)) == [("Levi's 501",)]


# Columns about people --------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # Only California has at least 5 customers.
        (f"SELECT state, COUNT(*) AS n FROM {USERS} GROUP BY state", [("California", 6)]),
        (
            f"SELECT u.state AS region, COUNT(*) FROM {USERS} AS u GROUP BY region",
            [("California", 6)],
        ),
        (f"SELECT COUNT(*) FROM {USERS} WHERE state = 'Texas'", []),
        (f"SELECT COUNT(*) FROM {USERS} WHERE state = 'California'", [(6,)]),
        (f"SELECT AVG(age) FROM {USERS} WHERE city = 'Town7'", []),
        # orders.gender is about people too: 6 women, 2 men.
        (f"SELECT gender, COUNT(*) FROM {ORDERS} GROUP BY gender", [("F", 6)]),
        # Each branch of a UNION hides its own small groups.
        (
            f"SELECT state AS v, COUNT(*) AS n FROM {USERS} GROUP BY state "
            f"UNION ALL SELECT city AS v, COUNT(*) AS n FROM {USERS} GROUP BY city",
            [("California", 6), ("Los Angeles", 6)],
        ),
        # Spend per customer by state: per-customer sums in a CTE, people columns at the end.
        (
            f"WITH s AS (SELECT user_id, SUM(sale_price) AS total FROM {ITEMS} GROUP BY user_id) "
            f"SELECT u.state, ROUND(AVG(s.total), 2) FROM s JOIN {USERS} AS u ON u.id = s.user_id "
            "GROUP BY u.state",
            [("California", 46.67)],
        ),
    ],
)
def test_small_groups_are_hidden(db, sql, expected):
    assert sorted(run(db, sql)) == expected


def test_small_groups_are_counted_within_the_brand_scope(db):
    # For Alice, Nevada has one customer (user 8), so it is hidden; California has six.
    sql = f"SELECT u.state, COUNT(DISTINCT u.id) FROM {USERS} AS u GROUP BY u.state"

    assert run(db, sql, brands=ALICE) == [("California", 6)]


def test_queries_with_people_columns_say_that_small_groups_are_hidden():
    sql = f"SELECT state, COUNT(*) FROM {USERS} GROUP BY state"

    assert check_sql(sql, dataset=DS, brands=None, max_rows=10).hides_small_groups
    assert not check_sql(
        f"SELECT COUNT(*) FROM {USERS}", dataset=DS, brands=None, max_rows=10
    ).hides_small_groups


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        (f"SELECT id, state FROM {USERS}", "must group"),
        (f"SELECT DISTINCT state FROM {USERS}", "must group"),
        (f"SELECT state, COUNT(*) OVER (PARTITION BY state) FROM {USERS}", "must group"),
        (f"SELECT state, ARRAY_AGG(id) FROM {USERS} GROUP BY state", "ARRAY_AGG"),
        (f"SELECT state, STRING_AGG(city, ',') FROM {USERS} GROUP BY state", "single rows"),
        (f"SELECT state, ANY_VALUE(age) FROM {USERS} GROUP BY state", "ANY_VALUE"),
        (f"SELECT state, MAX_BY(age, created_at) FROM {USERS} GROUP BY state", "single rows"),
        (f"SELECT id, state, COUNT(*) FROM {USERS} GROUP BY id, state", "customer ids"),
        (f"SELECT state, MIN(id) FROM {USERS} GROUP BY state", "customer ids"),
        (f"SELECT state, COUNT(IF(id = 5, 1, NULL)) FROM {USERS} GROUP BY state", "customer ids"),
        (f"SELECT state, COUNTIF(id = 5) FROM {USERS} GROUP BY state", "customer ids"),
        (f"SELECT COUNT(*) FROM {USERS} WHERE id = 1 AND state = 'Texas'", "customer ids"),
        (
            f"SELECT u.state, COUNT(*) FROM {ITEMS} AS oi JOIN {USERS} AS u "
            "ON u.id = oi.user_id AND u.id IN (1, 2, 3, 4, 5) GROUP BY u.state",
            "customer ids",
        ),
        (
            f"SELECT oi.user_id, u.state, SUM(oi.sale_price) FROM {ITEMS} AS oi "
            f"JOIN {USERS} AS u ON u.id = oi.user_id GROUP BY 1, 2",
            "customer ids",
        ),
        (
            f"WITH c AS (SELECT id, state FROM {USERS}) "
            "SELECT state, COUNT(*) FROM c GROUP BY state",
            "only in the final SELECT",
        ),
        (
            f"SELECT COUNT(*) FROM {ITEMS} WHERE user_id IN "
            f"(SELECT id FROM {USERS} WHERE state = 'Texas')",
            "only in the final SELECT",
        ),
        (
            f"SELECT n FROM (SELECT state, COUNT(*) AS n FROM {USERS} GROUP BY state) AS t",
            "only in the final SELECT",
        ),
        (
            # A people column name that comes from a CTE: no table with customer ids.
            f"WITH c AS (SELECT status AS state FROM {ORDERS}) "
            "SELECT state, COUNT(*) FROM c GROUP BY state",
            "read users, orders or order_items directly",
        ),
    ],
)
def test_ways_around_the_people_rules_are_rejected(sql, reason):
    assert reason in rejected(sql)
