"""Command line interface: `da-assistant check`, `chat` and `eval`."""

import logging
import tempfile
import uuid
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table

from da_assistant.agent import Agent, Confirmation, TurnResult
from da_assistant.config import ConfigError, Settings, UserProfile, load_settings, load_users
from da_assistant.errors import explain
from da_assistant.evals import (
    DailyQuotaReached,
    EvalOutcome,
    check_lines,
    group_results,
    load_questions,
    markdown_report,
    run_eval,
)
from da_assistant.health import run_checks
from da_assistant.metrics import summarize
from da_assistant.sql_tool import format_bytes
from da_assistant.tracing import TurnTrace, read_turns
from da_assistant.warehouse import Warehouse

app = typer.Typer(
    help="Data analysis chat assistant for the thelook_ecommerce dataset.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

# The eval cases: analysis questions and the red team.
EVAL_FILES = [Path("evals/questions.yaml"), Path("evals/red_team.yaml")]

CHAT_HELP = """Commands:
  /help      show this help
  /reports   list your saved reports
  /trace     show the last answer step by step
  /stats     show the metrics of this session
  /exit      end the session (Ctrl+D works too)"""


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show debug logs.")] = False,
) -> None:
    # The CLI shows errors to the user itself, so library logs and warnings
    # stay hidden by default.
    logging.captureWarnings(True)
    logging.basicConfig(
        level=logging.INFO if verbose else logging.CRITICAL,
        format="%(message)s",
        handlers=[RichHandler(console=Console(stderr=True), show_path=False)],
    )


@app.command()
def check() -> None:
    """Check the setup: the users file, BigQuery and Gemini."""
    settings = _settings()
    with console.status("Checking the setup..."):
        results = run_checks(settings)

    table = Table("", "Check", "Result", show_lines=True)
    for result in results:
        status = "[green]OK[/]" if result.ok else "[red]FAIL[/]"
        table.add_row(status, escape(result.name), escape(result.detail))
    console.print(table)
    if not all(result.ok for result in results):
        raise typer.Exit(1)


@app.command()
def chat(
    user: Annotated[str, typer.Option("--user", "-u", help="Your user id from config/users.yaml.")],
) -> None:
    """Start a chat session."""
    settings = _settings()
    profile = _user(settings, user)
    try:
        with console.status("Connecting to BigQuery and Gemini..."):
            agent = Agent.from_settings(settings, profile)
    except Exception as exc:  # setup errors end the session with a clear message
        _fail(explain(exc))
    thread_id = uuid.uuid4().hex

    console.print(f"Hi {escape(profile.name)}. You can analyze: {escape(profile.scope_text())}.")
    console.print("Ask about sales, customers or products. Type /help for commands.\n")

    while True:
        try:
            text = console.input("[bold cyan]you>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text in ("/exit", "/quit"):
            break
        if text == "/help":
            console.print(CHAT_HELP)
            continue
        if text == "/reports":
            _show_reports(agent, thread_id)
            continue
        if text == "/trace":
            _show_trace(agent.last_trace)
            continue
        if text == "/stats":
            _show_metrics(agent.tracer.turns, "This session")
            continue
        _answer(agent, text, thread_id)
    console.print("\nBye.")


@app.command("eval")
def eval_command(
    file: Annotated[
        list[Path] | None,
        typer.Option(help="A file of eval cases. Repeat for more. Default: both files in evals/."),
    ] = None,
    only: Annotated[
        list[str] | None, typer.Option(help="Run only the case with this id. Repeat for more.")
    ] = None,
    category: Annotated[
        list[str] | None,
        typer.Option(help="Run only the cases of this category. Repeat for more."),
    ] = None,
    report: Annotated[
        Path | None, typer.Option(help="Also write the results as markdown to this file.")
    ] = None,
    pause: Annotated[
        float, typer.Option(help="Seconds to wait between cases, for a key with a low rate limit.")
    ] = 0.0,
) -> None:
    """Ask the agent the eval cases and check the answers. Uses BigQuery and Gemini.

    Reports that the agent saves go to a temporary database, so the eval never changes
    the users' saved reports. Traces go to eval_traces.jsonl, next to the chat traces.
    """
    settings = _settings()
    users = _users(settings)
    questions = []
    for path in file or EVAL_FILES:
        try:
            questions += load_questions(path)
        except ConfigError as exc:
            _fail(str(exc))
    if only:
        questions = [question for question in questions if question.id in only]
    if category:
        questions = [question for question in questions if question.category in category]
    if not questions:
        _fail("No eval case matches --only and --category.")
    unknown = sorted({question.user for question in questions} - set(users))
    if unknown:
        _fail(f"Unknown users in the eval cases: {', '.join(unknown)}.")

    with tempfile.TemporaryDirectory() as tmp:
        eval_settings = settings.model_copy(
            update={
                "db_path": Path(tmp) / "eval.db",
                "trace_path": settings.trace_path.with_name("eval_traces.jsonl"),
            }
        )
        try:
            warehouse = Warehouse.from_settings(eval_settings)
        except Exception as exc:  # setup errors end the run with a clear message
            _fail(explain(exc))
        try:
            with console.status("Running the eval...") as status:
                outcomes = run_eval(
                    questions,
                    users=users,
                    warehouse=warehouse,
                    make_agent=lambda user: Agent.from_settings(eval_settings, user),
                    on_question=lambda question: status.update(escape(f"Asking: {question.id}")),
                    pause_s=pause,
                )
        except DailyQuotaReached as exc:
            _fail(f"{exc} No results were written.")

    table = Table("", "Case", "Model", "Checks", "Answer", show_lines=True)
    for outcome in outcomes:
        status_text = "[green]PASS[/]" if outcome.passed else "[red]FAIL[/]"
        table.add_row(status_text, escape(outcome.question.id), *_eval_cells(outcome))
    console.print(table)
    _show_groups(outcomes)
    _show_metrics([outcome.record for outcome in outcomes if outcome.record], "Eval metrics")
    if report:
        models = (
            f"Models: {settings.llm_model} is the primary model, {settings.llm_fallback_model} "
            f"the fallback, {settings.guard_model} the guard."
        )
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            markdown_report(outcomes, run_on=date.today(), models=models), encoding="utf-8"
        )
        console.print(f"Results written to {escape(str(report))}.")
    if not all(outcome.passed for outcome in outcomes):
        raise typer.Exit(1)


def _eval_cells(outcome: EvalOutcome) -> tuple[str, str, str]:
    if outcome.error:
        answer = f"ERROR: {outcome.error}"
    else:
        answer = outcome.turn.answer[:300] + "\n" + turn_summary(outcome.turn)
    return escape(outcome.model), escape("\n".join(check_lines(outcome))), escape(answer)


def _show_groups(outcomes: list[EvalOutcome]) -> None:
    table = Table("Suite or category", "Passed", "95% interval", "Stopped by")
    for row in group_results(outcomes):
        name = f"[bold]{escape(row.name)}[/]" if row.is_suite else "  " + escape(row.name)
        table.add_row(name, row.pass_text, row.interval_text, escape(row.stopped_text))
    console.print(table)


@app.command()
def stats(
    file: Annotated[Path | None, typer.Option(help="The trace file.")] = None,
) -> None:
    """Show the agent metrics of every turn in the trace file."""
    path = file or _settings().trace_path
    _show_metrics(read_turns(path), str(path))


def _show_metrics(turns: list[dict], title: str) -> None:
    table = Table("Metric", "Value", title=escape(title))
    for name, value in summarize(turns):
        table.add_row(name, value)
    console.print(table)
    console.print()


def _show_trace(trace: TurnTrace | None) -> None:
    if trace is None:
        console.print("No answer to show yet.\n")
        return
    record = trace.record
    console.print(
        f"[bold]Trace {trace.trace_id}[/] · {record.get('latency_ms', 0) / 1000:.1f} s · "
        f"${record.get('cost_usd', 0):.5f} · {escape(str(record.get('status', '')))}"
    )
    table = Table("Step", "ms", "What happened")
    for span in trace.spans:
        name = span["span"] if span["span"] != "tool" else f"tool: {span['tool']}"
        table.add_row(escape(name), str(span.get("ms", "")), escape(span_details(span)))
    console.print(table)
    console.print()


def span_details(span: dict) -> str:
    """One readable cell about a step of a trace."""
    kind = span["span"]
    tokens = f"{span.get('input_tokens', 0):,} in / {span.get('output_tokens', 0):,} out"
    if kind == "guard":
        text = f"{span.get('category')} ({span.get('source')}), {span.get('model')}, {tokens}"
        if span.get("model_errors"):
            text += f" (errors: {', '.join(span['model_errors'])})"
    elif kind == "agent":
        model = span.get("model", "?") + (" (fallback)" if span.get("fallback") else "")
        text = f"{model}, {_count(span.get('attempts', 1), 'attempt', 'attempts')}"
        if span.get("model_errors"):
            text += f" (errors: {', '.join(span['model_errors'])})"
        if span.get("skipped_primary"):
            text += " (primary model resting after a failure)"
        calls = span.get("tool_calls") or []
        text += f", {tokens} -> " + (", ".join(calls) if calls else "answer")
    elif kind == "tool" and span.get("tool") == "run_sql":
        text = f"{span.get('purpose', '')}\n{span.get('rows', 0)} rows, "
        text += f"{format_bytes(span.get('bytes', 0))}"
        if span.get("hidden_groups"):
            text += f", {span['hidden_groups']} small groups hidden"
        if span.get("masked_values"):
            text += f", {span['masked_values']} values masked"
        if span.get("error"):
            text += f"\nERROR: {span['error']}"
        text += f"\n{span.get('sql', '')[:300]}"
    elif kind == "tool":
        ids = span.get("report_ids") or ([span["report_id"]] if span.get("report_id") else [])
        text = f"reports {', '.join(f'#{i}' for i in ids) or 'none'}"
    elif kind == "confirm_delete":
        ids = ", ".join(f"#{i}" for i in span.get("report_ids", [])) or "none"
        text = f"{span.get('outcome')}: {ids}"
    elif kind == "final_check":
        answer = span.get("answer", {})
        text = f"answer {answer.get('chars', 0):,} chars, {span.get('masked_values', 0)} masked"
        if span.get("leak"):
            text += ", replaced: it quoted the instructions"
        if span.get("saved_report"):
            text += f", saved as report #{span['saved_report']}"
    else:
        text = ""
    if span.get("status") == "error":
        text += f"\nERROR: {span.get('error_type')}: {span.get('error', '')}"
    return text


def _answer(agent: Agent, question: str, thread_id: str) -> None:
    """Answer one question. Any error is shown, and the session goes on."""
    try:
        result = _working(
            lambda progress: agent.ask(question, thread_id=thread_id, on_progress=progress)
        )
        # A delete waits for the user. Only what the user types here can confirm it.
        while result.confirmation:
            typed = _ask_to_confirm(result.confirmation)
            result = _working(
                lambda progress, typed=typed: agent.resume(
                    typed, thread_id=thread_id, on_progress=progress
                )
            )
    except KeyboardInterrupt:
        console.print("[yellow]Stopped.[/]\n")
        return
    except Exception as exc:  # the chat must never crash on one bad turn
        console.print(f"[red]Sorry, I could not answer this.[/] {escape(explain(exc))}\n")
        return
    console.print(Markdown(result.answer or "I could not produce an answer. Please rephrase."))
    console.print(f"[dim]{escape(turn_summary(result))}[/]\n")


def _working(run: Callable[[Callable[[str], None]], TurnResult]) -> TurnResult:
    """Run a step of the agent with a spinner that shows its progress."""
    with console.status("Thinking...") as status:
        return run(lambda text: status.update(escape(text)))


def _ask_to_confirm(confirmation: Confirmation) -> str:
    """Show the exact reports a delete would remove, and return what the user types."""
    count = len(confirmation.reports)
    noun = "report" if count == 1 else "reports"
    console.print(f"\n[bold red]Delete {count} {noun} for good?[/] This cannot be undone.")
    table = Table("Id", "Title", "Saved")
    for report in confirmation.reports:
        saved = datetime.fromisoformat(report["created_at"]).astimezone()
        table.add_row(f"#{report['id']}", escape(report["title"]), f"{saved:%Y-%m-%d %H:%M}")
    console.print(table)
    expires = datetime.fromisoformat(confirmation.expires_at).astimezone()
    console.print(f"[dim]This request expires at {expires:%H:%M}.[/]")
    if confirmation.word == "yes":
        prompt = "Type [bold]yes[/] to delete, or anything else to keep them: "
    else:
        prompt = (
            f"Type [bold]{confirmation.word}[/] (the number of reports) to delete them, "
            "or anything else to keep them: "
        )
    try:
        return console.input(prompt)
    except (EOFError, KeyboardInterrupt):
        console.print()
        return ""


def _show_reports(agent: Agent, thread_id: str) -> None:
    try:
        reports = agent.saved_reports()
    except Exception as exc:  # the chat must never crash
        console.print(f"[red]Could not read your reports.[/] {escape(explain(exc))}\n")
        return
    if not reports:
        console.print("You have no saved reports.\n")
        return
    table = Table("Id", "Title", "Saved", "Conversation")
    for report in reports:
        where = "this one" if report.conversation_id == thread_id else "earlier"
        saved = f"{report.created_at.astimezone():%Y-%m-%d %H:%M}"
        table.add_row(f"#{report.id}", escape(report.title), saved, where)
    console.print(table)
    console.print()


_BLOCKED = {
    "off_topic": "blocked by the guard: off topic",
    "injection": "blocked by the guard: prompt injection",
    "leak": "answer replaced: it quoted the instructions",
}


def turn_summary(result: TurnResult) -> str:
    """One line with what the turn did and cost, shown under the answer."""
    parts = [_BLOCKED[result.blocked]] if result.blocked else []
    if result.queries or not result.blocked:
        failed = sum(
            1
            for query in result.queries
            if query["error"] or (query["rows"] == 0 and not query["hidden_groups"])
        )
        queries = _count(len(result.queries), "query", "queries")
        if failed:
            queries += f" ({failed} failed or empty)"
        parts.append(f"{queries}, {format_bytes(result.bytes_scanned)} scanned")
    if result.saved_report:
        parts.append(f"saved as report #{result.saved_report}")
    if result.deleted_reports:
        parts.append(_count(len(result.deleted_reports), "report deleted", "reports deleted"))
    if result.hidden_groups:
        parts.append(_count(result.hidden_groups, "small group hidden", "small groups hidden"))
    if result.masked_values:
        parts.append(_count(result.masked_values, "value masked", "values masked"))
    calls = _count(result.llm_calls, "model call", "model calls")
    if result.fallback:
        calls += " (fallback model)"
    parts.append(f"{calls}, {result.input_tokens:,} tokens in, {result.output_tokens:,} out")
    if result.cost_usd:
        parts.append(f"${result.cost_usd:.4f}")
    parts.append(f"{result.seconds:.1f} s")
    return " · ".join(parts)


def _count(number: int, one: str, many: str) -> str:
    return f"{number} {one if number == 1 else many}"


def _settings() -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        _fail(str(exc))


def _users(settings: Settings) -> dict[str, UserProfile]:
    try:
        return load_users(settings.users_file)
    except ConfigError as exc:
        _fail(str(exc))


def _user(settings: Settings, user_id: str) -> UserProfile:
    users = _users(settings)
    if user_id not in users:
        _fail(f"Unknown user '{user_id}'. Known users: {', '.join(users)}.")
    return users[user_id]


def _fail(message: str) -> NoReturn:
    console.print(f"[red]Error:[/] {escape(message)}")
    raise typer.Exit(2)
