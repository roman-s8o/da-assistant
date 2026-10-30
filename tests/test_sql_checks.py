import pytest

from da_assistant.sql_checks import SqlRejected, check_sql

DS = "bigquery-public-data.thelook_ecommerce"


def check(sql: str, max_rows: int = 1000) -> str:
    """The checked SQL for a user who sees all brands."""
    return check_sql(sql, dataset=DS, brands=None, max_rows=max_rows).sql


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT COUNT(*) FROM `{DS}.orders`",
        "SELECT order_id FROM `bigquery-public-data`.thelook_ecommerce.orders",
        f"WITH x AS (SELECT user_id FROM `{DS}.orders`) SELECT COUNT(*) FROM x",
        f"SELECT p.brand, SUM(oi.sale_price) FROM `{DS}.order_items` oi "
        f"JOIN `{DS}.products` p ON p.id = oi.product_id GROUP BY 1",
        f"SELECT id FROM `{DS}.users` UNION ALL SELECT id FROM `{DS}.products`",
        "SELECT 1",
    ],
)
def test_allowed_queries(sql):
    assert check(sql).startswith(("SELECT", "WITH"))


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        (f"CREATE TABLE p.d.t AS SELECT * FROM `{DS}.orders`", "read-only SELECT"),
        (f"DELETE FROM `{DS}.orders` WHERE TRUE", "read-only SELECT"),
        (f"INSERT INTO `{DS}.orders` (order_id) VALUES (1)", "read-only SELECT"),
        ("EXPORT DATA OPTIONS(uri='gs://b/*.csv', format='CSV') AS SELECT 1", "read-only SELECT"),
        ("SELECT 1; SELECT 2", "exactly one"),
        ("SELECT FROM WHERE", "could not be parsed"),
        ("", "exactly one"),
        (f"SELECT * FROM `{DS}.events`", "`bigquery-public-data.thelook_ecommerce.events`"),
        (f"SELECT * FROM `{DS}.INFORMATION_SCHEMA.TABLES`", "INFORMATION_SCHEMA"),
        ("SELECT * FROM `other-project.thelook_ecommerce.orders`", "other-project"),
        ("SELECT order_id FROM orders", "full names"),
        # A CTE name does not open a door to another table with the same name.
        (f"WITH orders AS (SELECT 1 AS n) SELECT n FROM `{DS}.events`", "events"),
        ("SELECT n FROM EXTERNAL_QUERY('conn', 'SELECT 1')", "Table functions"),
        (f"SELECT n FROM ML.PREDICT(MODEL `{DS}.m`, TABLE `{DS}.orders`)", "Table functions"),
        (f"SELECT order_id FROM `{DS}.*`", "not allowed"),
    ],
)
def test_rejected_queries(sql, reason):
    with pytest.raises(SqlRejected, match=reason):
        check(sql)


def test_adds_a_row_limit():
    assert check(f"SELECT order_id FROM `{DS}.orders`").endswith("LIMIT 1000")


def test_keeps_a_smaller_limit():
    assert check(f"SELECT order_id FROM `{DS}.orders` LIMIT 10").endswith("LIMIT 10")


def test_caps_a_larger_limit():
    assert check(f"SELECT order_id FROM `{DS}.orders` LIMIT 50000").endswith("LIMIT 1000")


def test_a_whole_statement_in_parentheses_is_fine():
    assert check(f"(SELECT order_id FROM `{DS}.orders`)").startswith("SELECT order_id")


def test_limit_applies_to_the_whole_union():
    sql = check("SELECT 1 AS n UNION ALL SELECT 2 AS n", max_rows=5)

    assert sql == "SELECT 1 AS n UNION ALL SELECT 2 AS n LIMIT 5"
