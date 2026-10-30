"""Command line interface: `da-assistant check` and `da-assistant chat`."""

import logging
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.table import Table

from da_assistant.config import ConfigError, Settings, UserProfile, load_settings, load_users
from da_assistant.health import run_checks

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
    console.print(f"Hi {escape(profile.name)}. You can analyze: {escape(profile.scope_text())}.")
    console.print("Type /help for commands.\n")

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
        console.print("[dim]The agent is not connected yet.[/]")
    console.print("\nBye.")


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
