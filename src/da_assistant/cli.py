"""Command line interface: `da-assistant check` and `da-assistant chat`."""

import logging
import uuid
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table

from da_assistant.agent import Agent, TurnResult
from da_assistant.config import ConfigError, Settings, UserProfile, load_settings, load_users
from da_assistant.errors import explain
from da_assistant.health import run_checks
from da_assistant.sql_tool import format_bytes

app = typer.Typer(
    help="Data analysis chat assistant for the thelook_ecommerce dataset.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

CHAT_HELP = """Commands:
  /help   show this help
  /exit   end the session (Ctrl+D works too)"""


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
        _answer(agent, text, thread_id)
    console.print("\nBye.")


def _answer(agent: Agent, question: str, thread_id: str) -> None:
    """Answer one question. Any error is shown, and the session goes on."""
    try:
        with console.status("Thinking...") as status:
            result = agent.ask(
                question, thread_id=thread_id, on_progress=lambda text: status.update(escape(text))
            )
    except KeyboardInterrupt:
        console.print("[yellow]Stopped.[/]\n")
        return
    except Exception as exc:  # the chat must never crash on one bad turn
        console.print(f"[red]Sorry, I could not answer this.[/] {escape(explain(exc))}\n")
        return
    console.print(Markdown(result.answer or "I could not produce an answer. Please rephrase."))
    console.print(f"[dim]{escape(turn_summary(result))}[/]\n")


def turn_summary(result: TurnResult) -> str:
    """One line with the cost of the turn, shown under the answer."""
    failed = sum(1 for query in result.queries if query["error"] or query["rows"] == 0)
    queries = _count(len(result.queries), "query", "queries")
    if failed:
        queries += f" ({failed} failed or empty)"
    return (
        f"{queries}, {format_bytes(result.bytes_scanned)} scanned · "
        f"{_count(result.llm_calls, 'model call', 'model calls')}, "
        f"{result.input_tokens:,} tokens in, {result.output_tokens:,} out · "
        f"{result.seconds:.1f} s"
    )


def _count(number: int, one: str, many: str) -> str:
    return f"{number} {one if number == 1 else many}"


def _settings() -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        _fail(str(exc))


def _user(settings: Settings, user_id: str) -> UserProfile:
    try:
        users = load_users(settings.users_file)
    except ConfigError as exc:
        _fail(str(exc))
    if user_id not in users:
        _fail(f"Unknown user '{user_id}'. Known users: {', '.join(users)}.")
    return users[user_id]


def _fail(message: str) -> NoReturn:
    console.print(f"[red]Error:[/] {escape(message)}")
    raise typer.Exit(2)
