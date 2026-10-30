"""The agent: a LangGraph loop of the model and the `run_sql` tool, with checks around it.

One turn (one question and its answer):

    START -> guard -> agent -> tools -> agent -> ... -> agent -> final_check -> END
               \\-> END (the guard blocked the message)

- guard: is this an analysis question, or off-topic, or a prompt injection?
- agent: the model decides which queries to run, so it can answer questions that need
  several steps.
- tools: every query goes through `SqlTool`, which checks, dry runs and runs it. A failed
  or empty query goes back to the model with the reason, so the model can fix it.
- final_check: masks anything that looks like personal data, replaces an answer that
  quotes the instructions, and adds how many small groups were hidden.

Each turn has a budget: model calls, failed queries and bytes scanned. When the budget
is used up, the model must answer without tools.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field

from da_assistant.config import Settings, UserProfile
from da_assistant.guard import Guard
from da_assistant.llm import chat_model
from da_assistant.privacy import mask_text
from da_assistant.prompts import guard_answer, system_prompt
from da_assistant.schema import column_types, schema_text
from da_assistant.sql_tool import SqlTool, hidden_groups_summary
from da_assistant.warehouse import Warehouse

TOOL_NAME = "run_sql"

GIVE_UP = (
    "Too many failed queries for this question. Do not run more queries. "
    "Tell the user plainly that you could not answer, and why."
)

# Questions and answers from earlier turns that the model still sees.
EARLIER_MESSAGES = 20


class RunSql(BaseModel):
    """Run one read-only BigQuery SQL query and get the result rows."""

    model_config = ConfigDict(title=TOOL_NAME)

    sql: str = Field(description="One BigQuery Standard SQL SELECT query.")
    purpose: str = Field(description="What this query finds out, in a few plain words.")


class QueryLog(TypedDict):
    purpose: str
    sql: str
    rows: int
    bytes: int
    error: str | None
    hidden_groups: int
    masked_values: int


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    # Counters for the current turn. Every new question resets them.
    llm_calls: int
    input_tokens: int
    output_tokens: int
    failed_queries: int
    bytes_scanned: int
    queries: list[QueryLog]
    # Values masked by the final check (the query results count their own).
    masked_values: int
    # Why the turn was stopped: "off_topic", "injection" (the guard) or "leak" (the answer
    # quoted the instructions). None for a normal turn.
    blocked: str | None


@dataclass(frozen=True)
class Budget:
    """Limits for one turn."""

    max_llm_calls: int
    max_sql_retries: int
    max_bytes: int


@dataclass(frozen=True)
class TurnResult:
    answer: str
    queries: list[QueryLog]
    llm_calls: int
    input_tokens: int
    output_tokens: int
    bytes_scanned: int
    seconds: float
    blocked: str | None = None
    hidden_groups: int = 0
    masked_values: int = 0


class Agent:
    def __init__(
        self,
        *,
        model: BaseChatModel,
        guard: Guard,
        sql_tool: SqlTool,
        user: UserProfile,
        schema: str,
        budget: Budget,
        rows_for_model: int,
        today: Callable[[], date] = date.today,
        checkpointer: BaseCheckpointSaver | None = None,
    ) -> None:
        self.model = model
        self.model_with_tools = model.bind_tools([RunSql])
        self.guard = guard
        self.sql_tool = sql_tool
        self.user = user
        self.schema = schema
        self.budget = budget
        self.rows_for_model = rows_for_model
        self.today = today
        self.graph = self._build_graph(checkpointer or InMemorySaver())

    @classmethod
    def from_settings(cls, settings: Settings, user: UserProfile) -> "Agent":
        warehouse = Warehouse.from_settings(settings)
        return cls(
            model=chat_model(settings),
            guard=Guard(chat_model(settings, settings.guard_model)),
            sql_tool=SqlTool(
                warehouse, brands=user.brand_scope, max_result_rows=settings.max_result_rows
            ),
            user=user,
            schema=schema_text(warehouse.dataset, column_types(warehouse)),
            budget=Budget(
                max_llm_calls=settings.max_llm_calls_per_turn,
                max_sql_retries=settings.max_sql_retries,
                max_bytes=settings.max_bytes_per_turn,
            ),
            rows_for_model=settings.rows_for_model,
        )

    def ask(
        self,
        question: str,
        *,
        thread_id: str,
        on_progress: Callable[[str], None] | None = None,
    ) -> TurnResult:
        """Answer one question. The thread id keeps the conversation history."""
        start = time.perf_counter()
        config = {"configurable": {"thread_id": thread_id}}
        turn: AgentState = {
            "messages": [HumanMessage(question)],
            "llm_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "failed_queries": 0,
            "bytes_scanned": 0,
            "queries": [],
            "masked_values": 0,
            "blocked": None,
        }
        for update in self.graph.stream(turn, config, stream_mode="updates"):
            if on_progress:
                _report_progress(update, on_progress)

        state = self.graph.get_state(config).values
        last = state["messages"][-1]
        queries = state["queries"]
        return TurnResult(
            answer=str(last.text).strip() if isinstance(last, AIMessage) else "",
            queries=queries,
            llm_calls=state["llm_calls"],
            input_tokens=state["input_tokens"],
            output_tokens=state["output_tokens"],
            bytes_scanned=state["bytes_scanned"],
            seconds=time.perf_counter() - start,
            blocked=state["blocked"],
            # The most groups one result hid. Results over the same groups hide the same ones.
            hidden_groups=max((query["hidden_groups"] for query in queries), default=0),
            masked_values=state["masked_values"] + sum(q["masked_values"] for q in queries),
        )

    def _build_graph(self, checkpointer: BaseCheckpointSaver):
        graph = StateGraph(AgentState)
        graph.add_node("guard", self._guard_node)
        graph.add_node("agent", self._agent_node)
        graph.add_node("tools", self._tools_node)
        graph.add_node("final_check", self._final_check_node)
        graph.add_edge(START, "guard")
        graph.add_conditional_edges("guard", _after_guard, ["agent", END])
        graph.add_conditional_edges("agent", _after_agent, ["tools", "final_check"])
        graph.add_edge("tools", "agent")
        graph.add_edge("final_check", END)
        return graph.compile(checkpointer=checkpointer)

    def _guard_node(self, state: AgentState) -> dict[str, Any]:
        messages = state["messages"]
        answers = [m for m in messages[:-1] if isinstance(m, AIMessage) and not m.tool_calls]
        result = self.guard.check(
            str(messages[-1].content),
            scope=self.user.scope_text(),
            last_answer=str(answers[-1].text) if answers else "",
        )
        update: dict[str, Any] = {
            "llm_calls": state["llm_calls"] + result.llm_calls,
            "input_tokens": state["input_tokens"] + result.input_tokens,
            "output_tokens": state["output_tokens"] + result.output_tokens,
        }
        if result.blocked:
            # A fixed reply: nothing in the message can change what the user gets back.
            update["messages"] = [AIMessage(result.reply)]
            update["blocked"] = result.category
        return update

    def _can_query(self, state: AgentState) -> bool:
        return (
            # Keep the last model call for the answer.
            state["llm_calls"] + 1 < self.budget.max_llm_calls
            and state["failed_queries"] <= self.budget.max_sql_retries
            and state["bytes_scanned"] < self.budget.max_bytes
        )

    def _agent_node(self, state: AgentState) -> dict[str, Any]:
        can_query = self._can_query(state)
        prompt = system_prompt(
            user=self.user,
            schema=self.schema,
            today=self.today(),
            can_query=can_query,
            rows_for_model=self.rows_for_model,
        )
        model = self.model_with_tools if can_query else self.model
        reply = model.invoke([SystemMessage(prompt), *conversation_view(state["messages"])])
        usage = reply.usage_metadata or {}
        return {
            "messages": [reply],
            "llm_calls": state["llm_calls"] + 1,
            "input_tokens": state["input_tokens"] + usage.get("input_tokens", 0),
            "output_tokens": state["output_tokens"] + usage.get("output_tokens", 0),
        }

    def _tools_node(self, state: AgentState) -> dict[str, Any]:
        failed = state["failed_queries"]
        scanned = state["bytes_scanned"]
        queries = list(state["queries"])
        replies = []
        for call in state["messages"][-1].tool_calls:
            if call["name"] == TOOL_NAME:
                sql = str(call["args"].get("sql", ""))
                result = self.sql_tool.run(sql, bytes_left=self.budget.max_bytes - scanned)
                scanned += result.bytes_scanned
                failed += 0 if result.useful else 1
                content = result.for_model(self.rows_for_model)
                queries.append(
                    QueryLog(
                        purpose=str(call["args"].get("purpose", "")),
                        sql=result.sql,
                        rows=result.row_count,
                        bytes=result.bytes_scanned,
                        error=result.error,
                        hidden_groups=result.hidden_groups,
                        masked_values=result.masked_values,
                    )
                )
            else:
                failed += 1
                content = f"ERROR: there is no tool named {call['name']}. Use {TOOL_NAME}."
            if failed > self.budget.max_sql_retries:
                content += f"\n\n{GIVE_UP}"
            replies.append(ToolMessage(content, tool_call_id=call["id"], name=call["name"]))
        return {
            "messages": replies,
            "failed_queries": failed,
            "bytes_scanned": scanned,
            "queries": queries,
        }

    def _final_check_node(self, state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        answer, masked = mask_text(str(last.text))
        answer, leaked = guard_answer(answer)
        hidden = [query["hidden_groups"] for query in state["queries"] if query["hidden_groups"]]
        if hidden and not leaked:
            answer += f"\n\n*Privacy note: {hidden_groups_summary(hidden)}*"
        update: dict[str, Any] = {"masked_values": state["masked_values"] + masked}
        if leaked:
            update["blocked"] = "leak"
        if answer != str(last.text):
            # Same id: the checked answer replaces the model's answer in the history too.
            update["messages"] = [AIMessage(answer, id=last.id)]
        return update


def conversation_view(messages: list[AnyMessage]) -> list[AnyMessage]:
    """The messages the model sees.

    The current turn is sent in full. From earlier turns, only the questions and the
    final answers are sent: old queries and results are dropped to keep the prompt small.
    """
    turn_start = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=0)
    earlier = [
        m
        for m in messages[:turn_start]
        if isinstance(m, HumanMessage) or (isinstance(m, AIMessage) and not m.tool_calls)
    ][-EARLIER_MESSAGES:]
    # The history must start with a question, not with an answer.
    while earlier and not isinstance(earlier[0], HumanMessage):
        earlier.pop(0)
    return earlier + messages[turn_start:]


def _after_guard(state: AgentState) -> str:
    return END if state["blocked"] else "agent"


def _after_agent(state: AgentState) -> str:
    last = state["messages"][-1]
    return "tools" if isinstance(last, AIMessage) and last.tool_calls else "final_check"


def _report_progress(update: dict[str, Any], on_progress: Callable[[str], None]) -> None:
    for node, values in update.items():
        if node == "agent":
            for call in values["messages"][-1].tool_calls:
                on_progress(f"Querying: {call['args'].get('purpose') or 'running a query'}")
        elif node == "tools":
            on_progress("Reading the results...")
