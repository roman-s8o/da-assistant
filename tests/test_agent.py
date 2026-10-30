from datetime import date

import pandas as pd
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from da_assistant.agent import GIVE_UP, Agent, Budget, conversation_view
from da_assistant.config import UserProfile
from da_assistant.sql_tool import QueryResult

USER = UserProfile(id="ceo", name="CEO", all_brands=True)
ROWS = pd.DataFrame({"orders": [124452]})


def query_call(sql="SELECT 1", purpose="count orders", call_id="c1"):
    call = {"name": "run_sql", "args": {"sql": sql, "purpose": purpose}, "id": call_id}
    return AIMessage("", tool_calls=[{**call, "type": "tool_call"}])


def answer(text):
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    return AIMessage(text, usage_metadata=usage)


def ok(rows=ROWS, bytes_scanned=1000):
    return QueryResult("SELECT 1 LIMIT 1000", rows=rows, bytes_scanned=bytes_scanned)


def failed(error="BigQuery: 400 Syntax error"):
    return QueryResult("SELEC 1", error=error)


class FakeModel:
    """Plays back scripted replies and records every call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []  # (tools were bound, messages)

    def bind_tools(self, tools):
        return _WithTools(self)

    def invoke(self, messages, with_tools=False):
        self.calls.append((with_tools, list(messages)))
        return self.replies.pop(0)


class _WithTools:
    def __init__(self, model):
        self.model = model

    def invoke(self, messages):
        return self.model.invoke(messages, with_tools=True)


class FakeSqlTool:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []  # (sql, bytes_left)

    def run(self, sql, *, bytes_left):
        self.calls.append((sql, bytes_left))
        return self.results.pop(0)


def make_agent(model, sql_tool, **limits):
    budget = Budget(**({"max_llm_calls": 8, "max_sql_retries": 2, "max_bytes": 10_000} | limits))
    return Agent(
        model=model,
        sql_tool=sql_tool,
        user=USER,
        schema="THE SCHEMA",
        budget=budget,
        rows_for_model=50,
        today=lambda: date(2026, 10, 30),
    )


def last_message(model, call_index):
    return model.calls[call_index][1][-1]


def test_answers_with_one_query():
    model = FakeModel(query_call(), answer("There are 124,452 orders."))

    result = make_agent(model, FakeSqlTool(ok())).ask("How many orders?", thread_id="t")

    assert result.answer == "There are 124,452 orders."
    assert result.llm_calls == 2
    assert (result.input_tokens, result.output_tokens) == (10, 5)
    assert result.bytes_scanned == 1000
    assert result.queries == [
        {
            "purpose": "count orders",
            "sql": "SELECT 1 LIMIT 1000",
            "rows": 1,
            "bytes": 1000,
            "error": None,
        }
    ]
    tool_message = last_message(model, 1)
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "c1"
    assert "124452" in tool_message.content


def test_system_prompt_has_schema_user_and_date():
    model = FakeModel(answer("We have four tables."))

    make_agent(model, FakeSqlTool()).ask("What data do we have?", thread_id="t")

    system = model.calls[0][1][0]
    assert isinstance(system, SystemMessage)
    assert "THE SCHEMA" in system.content
    assert "The user is CEO. They can analyze: all brands." in system.content
    assert "Friday, 30 October 2026" in system.content


def test_fixes_a_failed_query():
    model = FakeModel(query_call("SELEC 1"), query_call("SELECT 1", call_id="c2"), answer("Done."))

    result = make_agent(model, FakeSqlTool(failed(), ok())).ask("q", thread_id="t")

    assert result.answer == "Done."
    assert [q["error"] for q in result.queries] == ["BigQuery: 400 Syntax error", None]
    assert last_message(model, 1).content == "ERROR: BigQuery: 400 Syntax error"
    assert [with_tools for with_tools, _ in model.calls] == [True, True, True]


@pytest.mark.parametrize("bad_result", [failed(), ok(rows=pd.DataFrame({"orders": []}))])
def test_gives_up_after_the_retries(bad_result):
    model = FakeModel(
        query_call(call_id="c1"),
        query_call(call_id="c2"),
        query_call(call_id="c3"),
        answer("Sorry, I could not answer this."),
    )
    tool = FakeSqlTool(bad_result, bad_result, bad_result)

    result = make_agent(model, tool, max_sql_retries=2).ask("q", thread_id="t")

    # One try and two retries, then the model must answer without tools.
    assert len(tool.calls) == 3
    assert GIVE_UP not in last_message(model, 2).content
    assert GIVE_UP in last_message(model, 3).content
    with_tools, messages = model.calls[3]
    assert not with_tools
    assert "You cannot run more queries" in messages[0].content
    assert result.answer == "Sorry, I could not answer this."


def test_stops_at_the_model_call_budget():
    model = FakeModel(
        query_call(call_id="c1"), query_call(call_id="c2"), answer("A partial answer.")
    )

    result = make_agent(model, FakeSqlTool(ok(), ok()), max_llm_calls=3).ask("q", thread_id="t")

    assert result.llm_calls == 3
    # The last call is kept for the answer, so it has no tools.
    assert [with_tools for with_tools, _ in model.calls] == [True, True, False]


def test_byte_budget_shrinks_and_stops_queries():
    model = FakeModel(query_call(call_id="c1"), query_call(call_id="c2"), answer("x"))
    tool = FakeSqlTool(ok(bytes_scanned=4000), ok(bytes_scanned=6000))

    result = make_agent(model, tool, max_bytes=10_000).ask("q", thread_id="t")

    assert [bytes_left for _, bytes_left in tool.calls] == [10_000, 6000]
    assert result.bytes_scanned == 10_000
    assert [with_tools for with_tools, _ in model.calls] == [True, True, False]


def test_runs_parallel_queries():
    calls = query_call(call_id="c1").tool_calls + query_call(call_id="c2").tool_calls
    model = FakeModel(AIMessage("", tool_calls=calls), answer("x"))

    result = make_agent(model, FakeSqlTool(ok(), ok())).ask("q", thread_id="t")

    replies = model.calls[1][1][-2:]
    assert [m.tool_call_id for m in replies] == ["c1", "c2"]
    assert len(result.queries) == 2


def test_unknown_tool_is_an_error_for_the_model():
    bad = AIMessage("", tool_calls=[{"name": "drop_tables", "args": {}, "id": "c1"}])
    model = FakeModel(bad, answer("x"))

    make_agent(model, FakeSqlTool()).ask("q", thread_id="t")

    assert "no tool named drop_tables" in last_message(model, 1).content


def test_next_turn_sees_earlier_answers_but_not_old_queries():
    model = FakeModel(query_call(), answer("124,452 orders."), answer("Yes, it is a lot."))
    agent = make_agent(model, FakeSqlTool(ok()))

    agent.ask("How many orders?", thread_id="t")
    second = agent.ask("Is that a lot?", thread_id="t")

    sent = model.calls[2][1]
    assert [type(m).__name__ for m in sent] == [
        "SystemMessage",
        "HumanMessage",
        "AIMessage",
        "HumanMessage",
    ]
    assert sent[2].content == "124,452 orders."
    # The counters start again for each question.
    assert (second.llm_calls, second.queries, second.bytes_scanned) == (1, [], 0)


def test_threads_do_not_share_history():
    model = FakeModel(answer("one"), answer("two"))
    agent = make_agent(model, FakeSqlTool())

    agent.ask("first", thread_id="a")
    agent.ask("second", thread_id="b")

    assert [type(m).__name__ for m in model.calls[1][1]] == ["SystemMessage", "HumanMessage"]


def test_progress_shows_the_purpose_of_each_query():
    updates = []
    model = FakeModel(query_call(purpose="revenue by month"), answer("x"))

    make_agent(model, FakeSqlTool(ok())).ask("q", thread_id="t", on_progress=updates.append)

    assert updates == ["Querying: revenue by month", "Reading the results..."]


def test_conversation_view_starts_with_a_question():
    messages = [AIMessage("an answer without its question"), HumanMessage("new question")]

    assert conversation_view(messages) == [messages[1]]
