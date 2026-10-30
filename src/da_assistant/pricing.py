"""List prices, to estimate the cost of each question.

Checked on 2026-10-30 at https://ai.google.dev/gemini-api/docs/pricing and
https://cloud.google.com/bigquery/pricing. The free tiers (Gemini free tier, 1 TiB of
BigQuery queries a month) can make the real cost lower; this is the paid list price.
"""

# USD per 1M tokens: (input, output). Output includes thinking tokens.
MODEL_PRICES = {
    # Introductory price until 2026-12-31; $1.50 and $7.50 from 2027-01-01.
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
}

BIGQUERY_USD_PER_TIB = 6.25
# BigQuery bills at least 10 MB per table in a query. Counted here once per query.
BIGQUERY_MIN_BYTES = 10 * 2**20


def model_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """The cost of one model call. 0 for a model with no known price."""
    input_price, output_price = MODEL_PRICES.get(model, (0.0, 0.0))
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def query_cost(bytes_scanned: int) -> float:
    """The cost of one BigQuery query that ran. Dry runs and rejected queries are free."""
    if bytes_scanned <= 0:
        return 0.0
    return max(bytes_scanned, BIGQUERY_MIN_BYTES) / 2**40 * BIGQUERY_USD_PER_TIB
