"""The saved reports library, in SQLite.

Every read and every delete is filtered by the author: a user can see and delete only
their own reports.

Deleting is a two-step action:
1. `request_delete` finds the reports and stores a pending action with their exact ids
   and an expiry time. Nothing is deleted yet.
2. `resolve_delete` runs after the user answered the confirmation in the app. It deletes
   exactly the stored ids (not the filter again), so a report saved after the preview is
   never deleted by surprise. Deletion is final: there is no undo (client requirement).

The audit log keeps only metadata: who, when, what, how many and which ids. Never the
titles or the text of the reports. SQLite runs with `secure_delete`, so the text of a
deleted report does not stay behind in the database file.
"""

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_by_author ON reports (author);
CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author TEXT NOT NULL,
    kind TEXT NOT NULL,
    report_ids TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    author TEXT NOT NULL,
    action TEXT NOT NULL,
    report_ids TEXT NOT NULL,
    count INTEGER NOT NULL
);
"""


_AUDIT_ACTIONS = {
    "deleted": "reports deleted",
    "cancelled": "delete cancelled",
    "expired": "delete expired",
}


@dataclass(frozen=True)
class Report:
    id: int
    author: str
    conversation_id: str
    title: str
    content: str
    created_at: datetime


@dataclass(frozen=True)
class PendingDelete:
    action_id: int
    reports: tuple[Report, ...]
    expires_at: datetime


@dataclass(frozen=True)
class DeleteOutcome:
    # "deleted", "cancelled" or "expired"
    status: str
    report_ids: tuple[int, ...] = ()


def confirmation_word(count: int, *, count_above: int) -> str:
    """What the user must type to confirm: "yes", or the number of reports for many."""
    return str(count) if count > count_above else "yes"


class ReportStore:
    def __init__(
        self,
        path: Path,
        *,
        confirm_ttl_s: float = 300,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.path = path
        self.confirm_ttl = timedelta(seconds=confirm_ttl_s)
        self.now = now
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # A short connection per operation: simple and safe across threads.
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        # Overwrite deleted content with zeros. Without it, the text of a deleted report
        # stays in the free pages of the file until SQLite reuses them.
        db.execute("PRAGMA secure_delete = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, *, author: str, conversation_id: str, title: str, content: str) -> Report:
        created_at = self.now()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO reports (author, conversation_id, title, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (author, conversation_id, title, content, created_at.isoformat()),
            )
        return Report(cursor.lastrowid, author, conversation_id, title, content, created_at)

    def get(self, *, author: str, report_id: int) -> Report | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM reports WHERE id = ? AND author = ?", (report_id, author)
            ).fetchone()
        return _report(row) if row else None

    def find(
        self,
        *,
        author: str,
        report_ids: tuple[int, ...] = (),
        search: str = "",
        conversation_id: str | None = None,
    ) -> list[Report]:
        """The author's reports that match every given filter, oldest first."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM reports WHERE author = ? ORDER BY id", (author,)
            ).fetchall()
        reports = [_report(row) for row in rows]
        if report_ids:
            reports = [r for r in reports if r.id in report_ids]
        if search:
            # Case-insensitive for any language, which SQLite LIKE is not.
            needle = search.casefold()
            reports = [r for r in reports if needle in f"{r.title}\n{r.content}".casefold()]
        if conversation_id is not None:
            reports = [r for r in reports if r.conversation_id == conversation_id]
        return reports

    def request_delete(self, *, author: str, reports: list[Report]) -> PendingDelete:
        """Store a pending delete of exactly these reports. Nothing is deleted yet."""
        assert reports and all(r.author == author for r in reports)
        created_at = self.now()
        expires_at = created_at + self.confirm_ttl
        ids = [r.id for r in reports]
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO pending_actions "
                "(author, kind, report_ids, created_at, expires_at, status) "
                "VALUES (?, 'delete_reports', ?, ?, ?, 'pending')",
                (author, json.dumps(ids), created_at.isoformat(), expires_at.isoformat()),
            )
            self._audit(db, author, "delete requested", ids)
        return PendingDelete(cursor.lastrowid, tuple(reports), expires_at)

    def resolve_delete(self, *, author: str, action_id: int, confirmed: bool) -> DeleteOutcome:
        """Carry out or cancel a pending delete. Only the author's pending action counts."""
        with self._connect() as db:
            action = db.execute(
                "SELECT * FROM pending_actions WHERE id = ? AND author = ? AND status = 'pending'",
                (action_id, author),
            ).fetchone()
            if action is None:
                return DeleteOutcome("cancelled")
            ids = json.loads(action["report_ids"])
            if not confirmed:
                status = "cancelled"
            elif self.now() > datetime.fromisoformat(action["expires_at"]):
                status = "expired"
            else:
                status = "deleted"
            if status == "deleted":
                marks = ", ".join("?" for _ in ids)
                db.execute(
                    f"DELETE FROM reports WHERE author = ? AND id IN ({marks})", (author, *ids)
                )
            db.execute("UPDATE pending_actions SET status = ? WHERE id = ?", (status, action_id))
            self._audit(db, author, _AUDIT_ACTIONS[status], ids)
        return DeleteOutcome(status, tuple(ids) if status == "deleted" else ())

    def audit_log(self) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM audit_log ORDER BY id")]

    def _audit(self, db: sqlite3.Connection, author: str, action: str, ids: list[int]) -> None:
        db.execute(
            "INSERT INTO audit_log (at, author, action, report_ids, count) VALUES (?, ?, ?, ?, ?)",
            (self.now().isoformat(), author, action, json.dumps(ids), len(ids)),
        )


def _report(row: sqlite3.Row) -> Report:
    return Report(
        row["id"],
        row["author"],
        row["conversation_id"],
        row["title"],
        row["content"],
        datetime.fromisoformat(row["created_at"]),
    )
