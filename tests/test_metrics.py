import pytest

from da_assistant.metrics import percentile, summarize
from da_assistant.pricing import BIGQUERY_MIN_BYTES, model_cost, query_cost


def turn(**fields):
    return {"status": "ok", "answered": True, "latency_ms": 1000, "cost_usd": 0.001, **fields}


def metrics(turns):
    return dict(summarize(turns))


def test_success_rate_leaves_out_right_refusals_and_guard_blocks():
    turns = [
        turn(),
        turn(answered=False),
        turn(answered=False, blocked="off_topic"),
        turn(answered=False, refusal=True),
    ]

    result = metrics(turns)

    assert result["Answer success rate"] == "50% (1 of 2)"
    assert result["Guard blocks"] == "1"
    assert result["Refusals by the data rules"] == "1"


def test_guard_model_failures():
    turns = [turn(guard_source="model"), turn(guard_source="rules only"), turn()]

    assert metrics(turns)["Guard model failures (rules only)"] == "1"


def test_self_correction_rate():
    turns = [turn(failed_queries=1, self_corrected=True), turn(failed_queries=2), turn()]

    assert metrics(turns)["Self-correction rate"] == "50% (1 of 2)"


def test_latency_percentiles_and_cost():
    turns = [turn(latency_ms=ms, cost_usd=0.002) for ms in (1000, 2000, 3000, 4000, 20000)]

    result = metrics(turns)

    assert result["Latency p50 / p95"] == "3.0 s / 20.0 s"
    assert result["Cost per question (list price)"] == "$0.00200"


def test_no_turns():
    assert summarize([]) == [("Questions", "0")]


@pytest.mark.parametrize(
    ("values", "share", "expected"),
    [([5.0], 0.95, 5.0), ([1.0, 2.0, 3.0, 4.0], 0.5, 2.0), ([1.0, 2.0, 3.0, 4.0], 0.95, 4.0)],
)
def test_percentile(values, share, expected):
    assert percentile(values, share) == expected


def test_model_cost():
    assert model_cost("gemini-3.5-flash-lite", 1_000_000, 100_000) == pytest.approx(0.30 + 0.25)
    assert model_cost("unknown-model", 1_000_000, 1_000_000) == 0.0


def test_query_cost_has_a_minimum():
    assert query_cost(0) == 0.0
    assert query_cost(1) == query_cost(BIGQUERY_MIN_BYTES)
    assert query_cost(2**40) == pytest.approx(6.25)
