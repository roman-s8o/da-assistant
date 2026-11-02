import json
from pathlib import Path

import pandas as pd
import pytest
from fakes import (
    FakeGuard,
    FakeModel,
    FakeSqlTool,
    answer,
    failed,
    make_agent,
    ok,
    query_call,
    tool_call,
)

from da_assistant.reports import ReportStore
from da_assistant.resilience import CallInfo
from da_assistant.sql_tool import QueryResult, SqlTool
from da_assistant.tracing import Tracer, fingerprint, read_turns

QUESTION = "Which plan did we agree for the Zephyr-7731 launch?"
ANSWER = "The Quokka-5512 plan: cut prices by 30 percent in December."


def trace_file(tmp_path) -> tuple[Tracer, Path]:
    path = tmp_path / "traces.jsonl"
    return Tracer(path, user_id="ceo"), path


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_traces_hold_no_question_answer_or_report_text(tmp_path):
    tracer, path = trace_file(tmp_path)
    store = ReportStore(tmp_path / "reports.db")
    model = FakeModel(
        tool_call("save_report", title="Quokka plan"),
        answer(ANSWER),
        tool_call("delete_reports", all_reports=True),
        answer("Deleted."),
    )
    agent = make_agent(model, FakeSqlTool(), reports=store, tracer=tracer)

    result = agent.ask(QUESTION, thread_id="t")
    agent.ask("Delete all my reports", thread_id="t")
    agent.resume("yes", thread_id="t")

    text = path.read_text()
    for secret in ("Zephyr-7731", "Quokka-5512", "Quokka plan", "30 percent"):
        assert secret not in text
    turn = read_turns(path)[0]
    # A fingerprint can check a text someone shows you, without holding it.
    assert turn["question"] == fingerprint(QUESTION)
    assert turn["answer"] == fingerprint(result.answer)
    assert store.find(author="ceo") == []


def test_sql_and_errors_in_traces_are_masked(tmp_path):
    tracer, path = trace_file(tmp_path)
    sql = "SELECT 1 WHERE note = 'jane@example.com'"
    sql_result = QueryResult(sql, error="Query rejected: jane@example.com")
    model = FakeModel(query_call(sql=sql), answer("x"))

    make_agent(model, FakeSqlTool(sql_result), tracer=tracer).ask("q", thread_id="t")

    tool = next(r for r in records(path) if r.get("span") == "tool")
    assert "jane@example.com" not in path.read_text()
    assert tool["sql"] == "SELECT 1 WHERE note = '[hidden]'"
    assert tool["rejected"] is True
    # Nothing ran, so there is no checked SQL.
    assert tool["checked_sql"] is None


def test_each_step_is_a_span_and_the_turn_has_totals(tmp_path):
    tracer, path = trace_file(tmp_path)
    model = FakeModel(query_call(call_id="a"), query_call(call_id="b"), answer("Done."))
    tool = FakeSqlTool(failed(), ok(bytes_scanned=5_000_000))

    result = make_agent(model, tool, tracer=tracer).ask("q", thread_id="t")

    lines = records(path)
    assert [r.get("span", r["type"]) for r in lines] == [
        "guard",
        "agent",
        "tool",
        "agent",
        "tool",
        "agent",
        "final_check",
        "turn",
    ]
    turn = lines[-1]
    assert turn["trace_id"] == result.trace_id
    assert all(r["trace_id"] == result.trace_id for r in lines)
    assert (turn["status"], turn["answered"], turn["self_corrected"]) == ("ok", True, True)
    assert (turn["queries"], turn["failed_queries"], turn["rejected_queries"]) == (2, 1, 0)
    # One query that ran, billed at the 10 MB minimum.
    assert turn["cost_usd"] > 0


def test_refusal_is_counted_when_the_rules_reject_every_query(tmp_path):
    tracer, path = trace_file(tmp_path)
    rejected = QueryResult("SELECT email", error="Query rejected: `email` is personal data")
    model = FakeModel(query_call(), answer("I can't share emails."))

    make_agent(model, FakeSqlTool(rejected), tracer=tracer).ask("Emails?", thread_id="t")

    turn = read_turns(path)[0]
    assert (turn["refusal"], turn["answered"], turn["rejected_queries"]) == (True, False, 1)


def test_a_turn_that_fails_is_still_recorded(tmp_path):
    tracer, path = trace_file(tmp_path)

    class Down(FakeModel):
        def invoke(self, messages, with_tools=False):
            raise RuntimeError("503 UNAVAILABLE")

    with pytest.raises(RuntimeError):
        make_agent(Down(), FakeSqlTool(), tracer=tracer).ask("q", thread_id="t")

    agent_span = next(r for r in records(path) if r.get("span") == "agent")
    assert agent_span["status"] == "error"
    assert read_turns(path)[0]["status"] == "error"


def test_a_confirmation_is_one_turn_with_one_confirm_step(tmp_path):
    tracer, path = trace_file(tmp_path)
    store = ReportStore(tmp_path / "reports.db")
    store.save(author="ceo", conversation_id="t", title="Old", content="x")
    model = FakeModel(tool_call("delete_reports", all_reports=True), answer("Deleted."))
    agent = make_agent(model, FakeSqlTool(), reports=store, tracer=tracer)

    waiting = agent.ask("Delete all", thread_id="t")
    assert read_turns(path) == []
    done = agent.resume("yes", thread_id="t")

    spans = [r["span"] for r in records(path) if r["type"] == "span"]
    # LangGraph runs the confirmation step twice (pause, then resume): one span only.
    assert spans.count("confirm_delete") == 1
    turn = read_turns(path)[0]
    assert turn["trace_id"] == waiting.trace_id == done.trace_id
    assert turn["deleted_reports"] == list(done.deleted_reports)


def test_the_fallback_and_retries_show_in_the_turn(tmp_path):
    tracer, _ = trace_file(tmp_path)
    model = FakeModel(answer("x"))
    model.last_call = CallInfo("gemini-3.5-flash-lite", 2, True, ["503"], 1.0)

    agent = make_agent(model, FakeSqlTool(), tracer=tracer)
    result = agent.ask("q", thread_id="t")

    assert result.fallback
    turn = agent.last_trace.record
    assert (turn["fallback"], turn["retries"], turn["models"]) == (
        True,
        1,
        ["gemini-3.5-flash-lite"],
    )


def test_memory_only_tracer_keeps_the_session_turns():
    tracer = Tracer(None, user_id="ceo")

    make_agent(FakeModel(answer("x")), FakeSqlTool(), tracer=tracer).ask("q", thread_id="t")

    assert len(tracer.turns) == 1


def test_empty_results_and_hidden_groups(tmp_path):
    tracer, path = trace_file(tmp_path)
    empty = ok(rows=pd.DataFrame({"n": []}))
    model = FakeModel(query_call(call_id="a"), query_call(call_id="b"), answer("x"))

    make_agent(model, FakeSqlTool(empty, ok(hidden_groups=4)), tracer=tracer).ask(
        "q", thread_id="t"
    )

    turn = read_turns(path)[0]
    assert (turn["empty_results"], turn["hidden_groups"], turn["self_corrected"]) == (1, 4, True)


DS = "bigquery-public-data.thelook_ecommerce"
FOUR_TABLES = f"""
SELECT p.category, u.state, COUNT(DISTINCT o.order_id) AS orders, SUM(oi.sale_price) AS revenue
FROM `{DS}.order_items` AS oi
JOIN `{DS}.orders` AS o ON o.order_id = oi.order_id
JOIN `{DS}.products` AS p ON p.id = oi.product_id
JOIN `{DS}.users` AS u ON u.id = oi.user_id
WHERE oi.status NOT IN ('Cancelled', 'Returned') AND u.country = 'United States'
GROUP BY p.category, u.state
ORDER BY revenue DESC
"""


class FakeWarehouse:
    dataset = DS

    def __init__(self):
        self.ran = []

    def dry_run(self, sql):
        return 1000

    def run(self, sql):
        self.ran.append(sql)
        return pd.DataFrame({"category": ["Jeans"], "state": ["Texas"], "_hidden_groups": [3]})


def test_traces_keep_the_models_sql_of_a_four_table_join(tmp_path):
    tracer, path = trace_file(tmp_path)
    warehouse = FakeWarehouse()
    sql_tool = SqlTool(warehouse, brands=("Calvin Klein", "Diesel"), max_result_rows=1000)
    model = FakeModel(query_call(sql=FOUR_TABLES), answer("x"))

    agent = make_agent(model, sql_tool, tracer=tracer)
    agent.user = agent.user.model_copy(
        update={"brands": ("Calvin Klein", "Diesel"), "all_brands": False}
    )
    agent.ask("Revenue by category and state", thread_id="t")

    ran = warehouse.ran[0]
    # The SQL that ran starts with the safe views and the hidden groups count, so a cut at
    # 2000 characters loses the end of the model's query.
    assert len(ran) > 2000
    assert "ORDER BY revenue DESC" in ran and "ORDER BY revenue DESC" not in ran[:2000]
    tool = next(r for r in records(path) if r.get("span") == "tool")
    assert tool["sql"] == FOUR_TABLES
    assert tool["sql_chars"] == len(FOUR_TABLES)
    for part in ("WHERE oi.status", "u.country = 'United States'", "GROUP BY p.category, u.state"):
        assert part in tool["sql"]
    assert tool["checked_sql"] == fingerprint(ran)
    # The brand scope is in the turn record, so the rewrite can be done again.
    assert read_turns(path)[0]["brands"] == ["Calvin Klein", "Diesel"]


def test_the_turn_shows_when_the_guard_model_failed(tmp_path):
    tracer, _ = trace_file(tmp_path)
    guard = FakeGuard(source="rules only")

    agent = make_agent(FakeModel(answer("x")), FakeSqlTool(), guard=guard, tracer=tracer)
    agent.ask("q", thread_id="t")

    assert agent.last_trace.record["guard_source"] == "rules only"


def test_the_turn_shows_a_used_up_daily_quota(tmp_path):
    tracer, _ = trace_file(tmp_path)
    guard = FakeGuard(source="rules only", error="429 per day")

    agent = make_agent(FakeModel(answer("x")), FakeSqlTool(), guard=guard, tracer=tracer)
    agent.ask("q", thread_id="t")

    assert agent.last_trace.record["daily_quota"]
    guard_span = next(s for s in agent.last_trace.spans if s["span"] == "guard")
    assert guard_span["model_errors"] == ["429 per day"]


def test_a_normal_turn_has_no_daily_quota_flag(tmp_path):
    tracer, _ = trace_file(tmp_path)

    agent = make_agent(FakeModel(answer("x")), FakeSqlTool(), tracer=tracer)
    agent.ask("q", thread_id="t")

    assert agent.last_trace.record["daily_quota"] is False
