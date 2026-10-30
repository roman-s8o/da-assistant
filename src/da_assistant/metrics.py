"""Agent metrics, computed from turn records (one session, or a whole trace file)."""

import math
from typing import Any


def percentile(values: list[float], share: float) -> float:
    """The nearest-rank percentile, for example share=0.95 for p95."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = min(max(1, math.ceil(share * len(ordered))), len(ordered))
    return ordered[rank - 1]


def summarize(turns: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """The metrics as (name, value) rows."""
    count = len(turns)
    if count == 0:
        return [("Questions", "0")]
    blocked = [t for t in turns if t.get("blocked") in ("off_topic", "injection")]
    refusals = [t for t in turns if t.get("refusal")]
    # A guard block or a refusal by the data rules is the right outcome, not a failure.
    expected_answers = [
        t
        for t in turns
        if t.get("blocked") not in ("off_topic", "injection") and not t.get("refusal")
    ]
    answered = sum(1 for t in expected_answers if t.get("answered"))
    with_failures = [t for t in turns if t.get("failed_queries", 0) > 0]
    corrected = sum(1 for t in with_failures if t.get("self_corrected"))
    errors = sum(1 for t in turns if t.get("status") == "error")
    latency = [t.get("latency_ms", 0) / 1000 for t in turns]
    cost = [t.get("cost_usd", 0.0) for t in turns]
    queries = sum(t.get("queries", 0) for t in turns)
    tokens = sum(t.get("input_tokens", 0) + t.get("output_tokens", 0) for t in turns)

    def share(part: int, whole: int) -> str:
        return f"{part / whole:.0%} ({part} of {whole})" if whole else "-"

    return [
        ("Questions", str(count)),
        ("Answer success rate", share(answered, len(expected_answers))),
        ("Errors (the turn failed)", str(errors)),
        ("Self-correction rate", share(corrected, len(with_failures))),
        ("Queries", str(queries)),
        ("Empty results", str(sum(t.get("empty_results", 0) for t in turns))),
        (
            "Results with every group hidden",
            str(sum(t.get("all_hidden_results", 0) for t in turns)),
        ),
        ("Rejected queries", str(sum(t.get("rejected_queries", 0) for t in turns))),
        ("Refusals by the data rules", str(len(refusals))),
        ("Guard blocks", str(len(blocked))),
        (
            "Answers that quoted the instructions",
            str(sum(1 for t in turns if t.get("blocked") == "leak")),
        ),
        ("PII mask hits", str(sum(t.get("masked_values", 0) for t in turns))),
        ("Turns on the fallback model", share(sum(1 for t in turns if t.get("fallback")), count)),
        ("Model retries", str(sum(t.get("retries", 0) for t in turns))),
        (
            "Latency p50 / p95",
            f"{percentile(latency, 0.5):.1f} s / {percentile(latency, 0.95):.1f} s",
        ),
        ("Tokens per question", f"{tokens / count:,.0f}"),
        ("Cost per question (list price)", f"${sum(cost) / count:.5f}"),
        ("Total cost (list price)", f"${sum(cost):.4f}"),
    ]
