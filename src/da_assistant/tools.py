"""The tools the model can call, and the text the report tools give back to it.

The agent runs the tools (`agent.py`). Adding a capability (a chart, an email, a web
search) means adding a tool schema here and a handler in the agent.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from da_assistant.reports import DeleteOutcome, Report

# The note the app adds to an answer that was saved as a report.
SAVED_NOTE = "*Saved as report #{id}: {title}.*"


class RunSql(BaseModel):
    """Run one read-only BigQuery SQL query and get the result rows."""

    model_config = ConfigDict(title="run_sql")

    sql: str = Field(description="One BigQuery Standard SQL SELECT query.")
    purpose: str = Field(description="What this query finds out, in a few plain words.")


class SaveReport(BaseModel):
    """Save a report in the user's library of saved reports."""

    model_config = ConfigDict(title="save_report")

    title: str = Field(description="A short title, for example 'Q3 2026 sales report'.")
    which: Literal["this answer", "last answer"] = Field(
        default="this answer",
        description=(
            "'this answer': your final answer of this turn is the report, so write the full "
            "report after this call. 'last answer': save your previous answer as it is."
        ),
    )


class ListReports(BaseModel):
    """List the user's saved reports: id, title and date."""

    model_config = ConfigDict(title="list_reports")

    search: str = Field(default="", description="Only reports whose title or text has this.")
    this_conversation: bool = Field(
        default=False, description="Only reports saved in this conversation."
    )


class OpenReport(BaseModel):
    """Read the full text of one of the user's saved reports."""

    model_config = ConfigDict(title="open_report")

    report_id: int = Field(description="The report id, for example 12.")


class DeleteReports(BaseModel):
    """Ask to delete some of the user's saved reports.

    Nothing is deleted until the user confirms in the app. You get the result after that.
    """

    model_config = ConfigDict(title="delete_reports")

    report_ids: list[int] = Field(default_factory=list, description="Only these report ids.")
    search: str = Field(
        default="",
        description="Only reports whose title or text has this, for example a brand name.",
    )
    this_conversation: bool = Field(
        default=False, description="Only reports saved in this conversation."
    )
    all_reports: bool = Field(
        default=False, description="True only when the user asks to delete all their reports."
    )


TOOLS = (RunSql, SaveReport, ListReports, OpenReport, DeleteReports)


def report_list_text(reports: list[Report], conversation_id: str, limit: int = 50) -> str:
    if not reports:
        return "No saved reports match."
    lines = [f"{len(reports)} saved reports (id | title | saved at | conversation):"]
    for report in reports[:limit]:
        where = "this conversation" if report.conversation_id == conversation_id else "earlier"
        lines.append(
            f"#{report.id} | {report.title} | {report.created_at:%Y-%m-%d %H:%M} UTC | {where}"
        )
    if len(reports) > limit:
        lines.append(f"Showing the first {limit}. Use a search text to narrow the list.")
    return "\n".join(lines)


def report_text(report: Report | None, report_id: int) -> str:
    if report is None:
        # The same answer whether the report does not exist or belongs to someone else.
        return f"There is no report #{report_id} in the user's library."
    saved = f"{report.created_at:%Y-%m-%d %H:%M} UTC"
    return f"Report #{report.id}: {report.title} (saved {saved})\n\n{report.content}"


def delete_outcome_text(outcome: DeleteOutcome) -> str:
    if outcome.status == "deleted":
        ids = ", ".join(f"#{report_id}" for report_id in outcome.report_ids)
        return f"The user confirmed. Deleted {len(outcome.report_ids)} reports for good: {ids}."
    if outcome.status == "expired":
        return (
            "The confirmation came too late, so the request expired. Nothing was deleted. "
            "The user can ask again."
        )
    return "The user did not confirm. Nothing was deleted."


def without_saved_note(answer: str) -> str:
    """An answer without the app's "Saved as report" line, to save it again as is."""
    lines = [line for line in answer.splitlines() if not line.startswith("*Saved as report #")]
    return "\n".join(lines).strip()
