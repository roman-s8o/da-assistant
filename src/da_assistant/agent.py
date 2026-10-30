"""The agent: a LangGraph loop of the model and its tools, with checks around it.

One turn (one question and its answer):

    START -> guard -> agent -> tools -> agent -> ... -> agent -> final_check -> END
               |                 |
               |                 +-> confirm_delete -> agent   (waits for the user)
               +-> END (the guard blocked the message)

- guard: is this an analysis question, or off-topic, or a prompt injection?
- agent: the model decides which tools to call, so it can answer questions that need
  several steps.
- tools: `run_sql` goes through `SqlTool`, which checks, dry runs and runs the query.
  A failed or empty query goes back to the model with the reason, so it can fix it.
  The report tools read and write the user's library (`reports.py`).
- confirm_delete: a delete waits here until the user answers in the app. The model has
  no way to confirm: only the app can resume the graph with the user's answer.
- final_check: masks anything that looks like personal data, replaces an answer that
  quotes the instructions, adds how many small groups were hidden, and saves the
  answer as a report when the model asked for it.

Each turn has a budget: model calls, failed queries and bytes scanned. When the budget
is used up, the model must answer without tools.

Every step is traced (`tracing.py`): one span per node and per tool call, then one turn
record with the totals, the cost and the outcome. Traces hold no question, answer or
report text.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt

from da_assistant.config import Settings, UserProfile
from da_assistant.guard import Guard
from da_assistant.llm import resilient_model
from da_assistant.pricing import model_cost, query_cost
from da_assistant.privacy import mask_text
from da_assistant.prompts import guard_answer, system_prompt
from da_assistant.reports import Report, ReportStore, confirmation_word
from da_assistant.schema import column_types, schema_text
from da_assistant.sql_tool import SqlTool, hidden_groups_summary
from da_assistant.tools import (
    SAVED_NOTE,
    TOOLS,
    delete_outcome_text,
    report_list_text,
    report_text,
    without_saved_note,
)
from da_assistant.tracing import MAX_SQL_CHARS, Tracer, TurnTrace, fingerprint, safe_text
from da_assistant.warehouse import Warehouse

GIVE_UP = (
    "Too many failed queries for this question. Do not run more queries. "
    "Tell the user plainly that you could not answer, and why."
)

# Questions and answers from earlier turns that the model still sees.
EARLIER_MESSAGES = 20


class QueryLog(TypedDict):
    purpose: str
    # The SQL as the model wrote it.
    sql: str
    # The SQL that went to BigQuery, after the checks and the safe views. None if rejected.
    checked_sql: str | None
    rows: int
    bytes: int
    error: str | None
    hidden_groups: int
    masked_values: int


class PendingDeleteState(TypedDict):
    action_id: int
    call_id: str
    # What the user must type to confirm: "yes", or the number of reports.
    word: str
    expires_at: str
    reports: list[dict[str, Any]]


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
    # Reports: a title to save the final answer under, a delete that waits for the user,
    # and what was saved or deleted in this turn.
    save_title: str | None
    pending_delete: PendingDeleteState | None
    saved_report: int | None
    deleted_reports: list[int]


@dataclass(frozen=True)
class Budget:
    """Limits for one turn."""

    max_llm_calls: int
    max_sql_retries: int
    max_bytes: int


@dataclass(frozen=True)
class Confirmation:
    """A delete that waits for the user's answer. Resume the turn with `Agent.resume`."""

    reports: list[dict[str, Any]]
    word: str
    expires_at: str


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
    # Set when the turn stopped to ask the user to confirm a delete.
    confirmation: Confirmation | None = None
    saved_report: int | None = None
    deleted_reports: tuple[int, ...] = ()
    trace_id: str = ""
    cost_usd: float = 0.0
    fallback: bool = False


class Agent:
    def __init__(
        self,
        *,
        model: BaseChatModel,
        guard: Guard,
        sql_tool: SqlTool,
        reports: ReportStore,
        user: UserProfile,
        schema: str,
        budget: Budget,
        rows_for_model: int,
        confirm_count_above: int = 3,
        tracer: Tracer | None = None,
        today: Callable[[], date] = date.today,
        checkpointer: BaseCheckpointSaver | None = None,
    ) -> None:
        self.model = model
        self.model_with_tools = model.bind_tools(list(TOOLS))
        self.guard = guard
        self.sql_tool = sql_tool
        self.reports = reports
        self.user = user
        self.schema = schema
        self.budget = budget
        self.rows_for_model = rows_for_model
        self.confirm_count_above = confirm_count_above
        self.tracer = tracer or Tracer(None, user_id=user.id)
        self.today = today
        self.graph = self._build_graph(checkpointer or InMemorySaver())
        # The trace of the turn in progress, and of the last finished turn (for /trace).
        self._turn: TurnTrace | None = None
        self.last_trace: TurnTrace | None = None

    @classmethod
    def from_settings(cls, settings: Settings, user: UserProfile) -> "Agent":
        warehouse = Warehouse.from_settings(settings)
        return cls(
            model=resilient_model(settings, settings.llm_model, settings.llm_fallback_model),
            guard=Guard(
                resilient_model(
                    settings, settings.guard_model, None, timeout_s=settings.guard_timeout_s
                ),
                model_name=settings.guard_model,
            ),
            sql_tool=SqlTool(
                warehouse, brands=user.brand_scope, max_result_rows=settings.max_result_rows
            ),
            reports=ReportStore(settings.db_path, confirm_ttl_s=settings.confirm_ttl_s),
            user=user,
            schema=schema_text(warehouse.dataset, column_types(warehouse)),
            budget=Budget(
                max_llm_calls=settings.max_llm_calls_per_turn,
                max_sql_retries=settings.max_sql_retries,
                max_bytes=settings.max_bytes_per_turn,
            ),
            rows_for_model=settings.rows_for_model,
            confirm_count_above=settings.confirm_count_above,
            tracer=Tracer(settings.trace_path, user_id=user.id),
        )

    def ask(
        self,
        question: str,
        *,
        thread_id: str,
        on_progress: Callable[[str], None] | None = None,
    ) -> TurnResult:
        """Answer one question. The thread id keeps the conversation history."""
        config = _config(thread_id)
        # A new question drops a delete that still waits for an answer: cancel it.
        waiting = self.graph.get_state(config)
        if waiting.interrupts and waiting.values.get("pending_delete"):
            action_id = waiting.values["pending_delete"]["action_id"]
            self.reports.resolve_delete(author=self.user.id, action_id=action_id, confirmed=False)
            if self._turn is not None:
                self._finish(waiting.values, status="cancelled")
        self._turn = self.tracer.new_turn(thread_id)
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
            "save_title": None,
            "pending_delete": None,
            "saved_report": None,
            "deleted_reports": [],
        }
        return self._run(turn, config, on_progress)

    def resume(
        self,
        typed: str,
        *,
        thread_id: str,
        on_progress: Callable[[str], None] | None = None,
    ) -> TurnResult:
        """Go on with a turn that waits for a confirmation, with what the user typed."""
        if self._turn is None:
            self._turn = self.tracer.new_turn(thread_id)
        return self._run(Command(resume=typed), _config(thread_id), on_progress)

    def saved_reports(self) -> list[Report]:
        return self.reports.find(author=self.user.id)

    def _run(self, graph_input: Any, config: RunnableConfig, on_progress) -> TurnResult:
        start = time.perf_counter()
        turn = self._turn
        try:
            for update in self.graph.stream(graph_input, config, stream_mode="updates"):
                if on_progress:
                    _report_progress(update, on_progress)
        except Exception as exc:
            turn.busy_s += time.perf_counter() - start
            self._finish(self.graph.get_state(config).values, status="error", error=exc)
            raise
        turn.busy_s += time.perf_counter() - start

        snapshot = self.graph.get_state(config)
        state = snapshot.values
        confirmation = None
        if snapshot.interrupts:
            confirmation = Confirmation(**snapshot.interrupts[0].value)
            record = {}
        else:
            record = self._finish(state, status="ok")
        last = state["messages"][-1]
        queries = state["queries"]
        return TurnResult(
            answer=str(last.text).strip()
            if isinstance(last, AIMessage) and not confirmation
            else "",
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
            confirmation=confirmation,
            saved_report=state["saved_report"],
            deleted_reports=tuple(state["deleted_reports"]),
            trace_id=turn.trace_id,
            cost_usd=record.get("cost_usd", 0.0),
            fallback=record.get("fallback", False),
        )

    def _finish(self, state: dict[str, Any], *, status: str, error=None) -> dict[str, Any]:
        """Write the turn record with its totals and outcome, and keep it for /trace."""
        turn, self._turn = self._turn, None
        record = turn_record(
            turn, state, status=status, max_sql_retries=self.budget.max_sql_retries
        )
        # With the model's SQL, the brand scope is all it takes to redo the rewrite.
        record["brands"] = list(self.user.brand_scope) if self.user.brand_scope else "all"
        if error is not None:
            record["error"] = safe_text(type(error).__name__ + ": " + str(error))
        self.last_trace = turn
        return turn.finish(**record)

    def _build_graph(self, checkpointer: BaseCheckpointSaver):
        graph = StateGraph(AgentState)
        graph.add_node("guard", self._guard_node)
        graph.add_node("agent", self._agent_node)
        graph.add_node("tools", self._tools_node)
        graph.add_node("confirm_delete", self._confirm_delete_node)
        graph.add_node("final_check", self._final_check_node)
        graph.add_edge(START, "guard")
        graph.add_conditional_edges("guard", _after_guard, ["agent", END])
        graph.add_conditional_edges("agent", _after_agent, ["tools", "final_check"])
        graph.add_conditional_edges("tools", _after_tools, ["confirm_delete", "agent"])
        graph.add_edge("confirm_delete", "agent")
        graph.add_edge("final_check", END)
        return graph.compile(checkpointer=checkpointer)

    def _guard_node(self, state: AgentState) -> dict[str, Any]:
        messages = state["messages"]
        question = str(messages[-1].content)
        with self._turn.span("guard", model=self.guard.model_name) as span:
            result = self.guard.check(
                question, scope=self.user.scope_text(), last_answer=_last_answer(messages[:-1])
            )
            span.update(
                question=fingerprint(question),
                category=result.category,
                source=result.source,
                llm_calls=result.llm_calls,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
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
        with self._turn.span("agent", with_tools=can_query) as span:
            try:
                reply = model.invoke([SystemMessage(prompt), *conversation_view(state["messages"])])
            finally:
                span.update(_call_fields(model))
            usage = reply.usage_metadata or {}
            span.update(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                tool_calls=[call["name"] for call in reply.tool_calls],
            )
        return {
            "messages": [reply],
            "llm_calls": state["llm_calls"] + 1,
            "input_tokens": state["input_tokens"] + usage.get("input_tokens", 0),
            "output_tokens": state["output_tokens"] + usage.get("output_tokens", 0),
        }

    def _tools_node(self, state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        conversation_id = config["configurable"]["thread_id"]
        update: dict[str, Any] = {
            "failed_queries": state["failed_queries"],
            "bytes_scanned": state["bytes_scanned"],
            "queries": list(state["queries"]),
            "save_title": state["save_title"],
            "pending_delete": None,
            "saved_report": state["saved_report"],
        }
        replies = []
        for call in state["messages"][-1].tool_calls:
            name, args = call["name"], call["args"]
            with self._turn.span("tool", tool=name) as span:
                if name == "run_sql":
                    content = self._run_sql(args, update, span)
                elif name == "save_report":
                    content = self._save_report(args, state, update, conversation_id, span)
                elif name == "list_reports":
                    content = self._list_reports(args, conversation_id, span)
                elif name == "open_report":
                    content = self._open_report(args, span)
                elif name == "delete_reports":
                    content = self._request_delete(call, update, conversation_id, span)
                else:
                    update["failed_queries"] += 1
                    content = f"ERROR: there is no tool named {name}."
                    span.update(status="unknown tool")
            # A pending delete answers its call later, after the user's confirmation.
            if content is not None:
                replies.append(ToolMessage(content, tool_call_id=call["id"], name=name))
        update["messages"] = replies
        return update

    def _run_sql(self, args: dict[str, Any], update: dict[str, Any], span: dict[str, Any]) -> str:
        bytes_left = self.budget.max_bytes - update["bytes_scanned"]
        model_sql = str(args.get("sql", ""))
        result = self.sql_tool.run(model_sql, bytes_left=bytes_left)
        rejected = bool(result.error and result.error.startswith("Query rejected"))
        checked_sql = None if rejected else result.sql
        span.update(
            purpose=safe_text(str(args.get("purpose", ""))),
            # The model's own SQL: short, and it shows its WHERE and GROUP BY. The SQL that
            # ran starts with the safe views, so a cut would lose them. It follows from this
            # SQL and the user's brand scope (in the turn record); its fingerprint is kept.
            sql=safe_text(model_sql, MAX_SQL_CHARS),
            sql_chars=len(model_sql),
            checked_sql=fingerprint(checked_sql) if checked_sql else None,
            rows=result.row_count,
            bytes=result.bytes_scanned,
            error=safe_text(result.error) if result.error else None,
            rejected=rejected,
            hidden_groups=result.hidden_groups,
            masked_values=result.masked_values,
        )
        update["bytes_scanned"] += result.bytes_scanned
        update["failed_queries"] += 0 if result.useful else 1
        update["queries"].append(
            QueryLog(
                purpose=str(args.get("purpose", "")),
                sql=model_sql,
                checked_sql=checked_sql,
                rows=result.row_count,
                bytes=result.bytes_scanned,
                error=result.error,
                hidden_groups=result.hidden_groups,
                masked_values=result.masked_values,
            )
        )
        content = result.for_model(self.rows_for_model)
        if update["failed_queries"] > self.budget.max_sql_retries:
            content += f"\n\n{GIVE_UP}"
        return content

    def _save_report(
        self,
        args: dict[str, Any],
        state: AgentState,
        update: dict[str, Any],
        conversation_id: str,
        span: dict[str, Any],
    ) -> str:
        title = " ".join(str(args.get("title", "")).split())[:120] or "Report"
        if args.get("which") == "last answer":
            turn_start = _turn_start(state["messages"])
            previous = without_saved_note(_last_answer(state["messages"][:turn_start]))
            if not previous:
                return "ERROR: there is no earlier answer to save."
            report = self.reports.save(
                author=self.user.id, conversation_id=conversation_id, title=title, content=previous
            )
            update["saved_report"] = report.id
            span.update(which="last answer", report_id=report.id)
            return f"Saved the last answer as report #{report.id}: {title}."
        update["save_title"] = title
        span.update(which="this answer")
        return (
            "OK. Your final answer of this turn will be saved as a report with this title. "
            "Write the full report now."
        )

    def _list_reports(
        self, args: dict[str, Any], conversation_id: str, span: dict[str, Any]
    ) -> str:
        reports = self.reports.find(
            author=self.user.id,
            search=str(args.get("search") or "").strip(),
            conversation_id=conversation_id if args.get("this_conversation") else None,
        )
        span.update(report_ids=[report.id for report in reports])
        return report_list_text(reports, conversation_id)

    def _open_report(self, args: dict[str, Any], span: dict[str, Any]) -> str:
        try:
            report_id = int(args.get("report_id"))
        except (TypeError, ValueError):
            return "ERROR: report_id must be a number."
        report = self.reports.get(author=self.user.id, report_id=report_id)
        span.update(report_id=report_id, found=report is not None)
        return report_text(report, report_id)

    def _request_delete(
        self,
        call: dict[str, Any],
        update: dict[str, Any],
        conversation_id: str,
        span: dict[str, Any],
    ) -> str | None:
        args = call["args"]
        try:
            report_ids = tuple(int(i) for i in args.get("report_ids") or ())
        except (TypeError, ValueError):
            return "ERROR: report_ids must be numbers."
        search = str(args.get("search") or "").strip()
        this_conversation = bool(args.get("this_conversation"))
        if not (report_ids or search or this_conversation or args.get("all_reports")):
            return (
                "ERROR: say which reports to delete: report_ids, a search text, "
                "this_conversation, or all_reports when the user asks for all of them."
            )
        if update["pending_delete"]:
            return "ERROR: one delete at a time. The user must answer the first one."
        reports = self.reports.find(
            author=self.user.id,
            report_ids=report_ids,
            search=search,
            conversation_id=conversation_id if this_conversation else None,
        )
        span.update(report_ids=[report.id for report in reports])
        if not reports:
            return "No saved reports of the user match, so nothing needs to be deleted."
        pending = self.reports.request_delete(author=self.user.id, reports=reports)
        span.update(pending_action=pending.action_id)
        update["pending_delete"] = PendingDeleteState(
            action_id=pending.action_id,
            call_id=call["id"],
            word=confirmation_word(len(reports), count_above=self.confirm_count_above),
            expires_at=pending.expires_at.isoformat(),
            reports=[
                {"id": r.id, "title": r.title, "created_at": r.created_at.isoformat()}
                for r in reports
            ],
        )
        return None

    def _confirm_delete_node(self, state: AgentState) -> dict[str, Any]:
        pending = state["pending_delete"]
        # The graph stops here until the app resumes it with what the user typed.
        # LangGraph runs this node again from the start on resume, so nothing above this
        # line may change anything.
        typed = interrupt(
            {
                "reports": pending["reports"],
                "word": pending["word"],
                "expires_at": pending["expires_at"],
            }
        )
        confirmed = str(typed).strip().lower() == pending["word"]
        with self._turn.span("confirm_delete", pending_action=pending["action_id"]) as span:
            outcome = self.reports.resolve_delete(
                author=self.user.id, action_id=pending["action_id"], confirmed=confirmed
            )
            span.update(outcome=outcome.status, report_ids=list(outcome.report_ids))
        reply = ToolMessage(
            delete_outcome_text(outcome), tool_call_id=pending["call_id"], name="delete_reports"
        )
        return {
            "messages": [reply],
            "pending_delete": None,
            "deleted_reports": state["deleted_reports"] + list(outcome.report_ids),
        }

    def _final_check_node(self, state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        with self._turn.span("final_check") as span:
            update = self._final_check(state, config)
            answer = (
                update["messages"][0].text if "messages" in update else state["messages"][-1].text
            )
            span.update(
                answer=fingerprint(str(answer)),
                masked_values=update["masked_values"] - state["masked_values"],
                leak=update.get("blocked") == "leak",
                saved_report=update.get("saved_report"),
            )
        return update

    def _final_check(self, state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        last = state["messages"][-1]
        answer, masked = mask_text(str(last.text))
        answer, leaked = guard_answer(answer)
        hidden = [query["hidden_groups"] for query in state["queries"] if query["hidden_groups"]]
        if hidden and not leaked:
            answer += f"\n\n*Privacy note: {hidden_groups_summary(hidden)}*"
        update: dict[str, Any] = {"masked_values": state["masked_values"] + masked}
        if leaked:
            update["blocked"] = "leak"
        title = state["save_title"]
        if title and not leaked and answer.strip():
            # The report is the checked answer: the same text the user sees.
            report = self.reports.save(
                author=self.user.id,
                conversation_id=config["configurable"]["thread_id"],
                title=title,
                content=answer,
            )
            update["saved_report"] = report.id
            answer += "\n\n" + SAVED_NOTE.format(id=report.id, title=title)
        if answer != str(last.text):
            # Same id: the checked answer replaces the model's answer in the history too.
            update["messages"] = [AIMessage(answer, id=last.id)]
        return update


def conversation_view(messages: list[AnyMessage]) -> list[AnyMessage]:
    """The messages the model sees.

    The current turn is sent in full. From earlier turns, only the questions and the
    final answers are sent: old queries and results are dropped to keep the prompt small.
    """
    turn_start = _turn_start(messages)
    earlier = [
        m
        for m in messages[:turn_start]
        if isinstance(m, HumanMessage) or (isinstance(m, AIMessage) and not m.tool_calls)
    ][-EARLIER_MESSAGES:]
    # The history must start with a question, not with an answer.
    while earlier and not isinstance(earlier[0], HumanMessage):
        earlier.pop(0)
    return earlier + messages[turn_start:]


def turn_record(
    turn: TurnTrace, state: dict[str, Any], *, status: str, max_sql_retries: int
) -> dict[str, Any]:
    """The totals and the outcome of a turn, from its state and its spans. No text."""
    queries = state.get("queries", [])
    outcomes = []  # "useful", "failed" or "empty", in order
    for query in queries:
        if query["error"]:
            outcomes.append("failed")
        elif query["rows"] == 0 and not query["hidden_groups"]:
            outcomes.append("empty")
        else:
            outcomes.append("useful")
    first_problem = next((i for i, o in enumerate(outcomes) if o != "useful"), None)
    self_corrected = first_problem is not None and "useful" in outcomes[first_problem:]
    rejected = sum(1 for q in queries if (q["error"] or "").startswith("Query rejected"))
    useful = outcomes.count("useful")
    refusal = rejected > 0 and useful == 0
    gave_up = state.get("failed_queries", 0) > max_sql_retries
    blocked = state.get("blocked")
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    answer = str(last.text) if isinstance(last, AIMessage) and not last.tool_calls else ""

    model_spans = [s for s in turn.spans if s["span"] in ("guard", "agent")]
    cost = sum(
        model_cost(s.get("model", ""), s.get("input_tokens", 0), s.get("output_tokens", 0))
        for s in model_spans
    ) + sum(query_cost(q["bytes"]) for q in queries if not q["error"])
    agent_spans = [s for s in turn.spans if s["span"] == "agent"]
    return {
        "status": status,
        "question": next(
            (s["question"] for s in turn.spans if s["span"] == "guard"), fingerprint("")
        ),
        "answer": fingerprint(answer),
        "answered": status == "ok" and not blocked and not refusal and not gave_up and bool(answer),
        "blocked": blocked,
        "refusal": refusal,
        "gave_up": gave_up,
        "llm_calls": state.get("llm_calls", 0),
        "input_tokens": state.get("input_tokens", 0),
        "output_tokens": state.get("output_tokens", 0),
        "models": sorted({s.get("model", "") for s in agent_spans}),
        "fallback": any(s.get("fallback") for s in agent_spans),
        "retries": sum(max(0, s.get("attempts", 1) - 1) for s in agent_spans),
        "queries": len(queries),
        "failed_queries": outcomes.count("failed") + outcomes.count("empty"),
        "rejected_queries": rejected,
        "empty_results": outcomes.count("empty"),
        "self_corrected": self_corrected,
        "bytes_scanned": state.get("bytes_scanned", 0),
        "hidden_groups": max((q["hidden_groups"] for q in queries), default=0),
        "masked_values": state.get("masked_values", 0) + sum(q["masked_values"] for q in queries),
        "saved_report": state.get("saved_report"),
        "deleted_reports": list(state.get("deleted_reports", [])),
        "cost_usd": round(cost, 6),
    }


def _call_fields(model: Any) -> dict[str, Any]:
    """Which model answered, with how many attempts, for the agent span."""
    call = getattr(model, "last_call", None)
    if call is None:
        name = getattr(model, "model", None)
        return {"model": name if isinstance(name, str) else "unknown"}
    return {
        "model": call.model,
        "fallback": call.fallback,
        "attempts": call.attempts,
        "model_errors": call.errors,
        "skipped_primary": call.skipped_primary,
        "model_seconds": round(call.seconds, 2),
    }


def _turn_start(messages: list[AnyMessage]) -> int:
    return max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=0)


def _last_answer(messages: list[AnyMessage]) -> str:
    answers = [m for m in messages if isinstance(m, AIMessage) and not m.tool_calls]
    return str(answers[-1].text) if answers else ""


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def _after_guard(state: AgentState) -> str:
    return END if state["blocked"] else "agent"


def _after_agent(state: AgentState) -> str:
    last = state["messages"][-1]
    return "tools" if isinstance(last, AIMessage) and last.tool_calls else "final_check"


def _after_tools(state: AgentState) -> str:
    return "confirm_delete" if state["pending_delete"] else "agent"


def _report_progress(update: dict[str, Any], on_progress: Callable[[str], None]) -> None:
    for node, values in update.items():
        if node == "agent":
            for call in values["messages"][-1].tool_calls:
                if call["name"] == "run_sql":
                    on_progress(f"Querying: {call['args'].get('purpose') or 'running a query'}")
                else:
                    on_progress(f"Working on saved reports: {call['name']}")
        elif node == "tools":
            on_progress("Reading the results...")
