from datetime import UTC, datetime, timedelta

import pytest

from da_assistant.reports import ReportStore, confirmation_word


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return ReportStore(tmp_path / "data" / "reports.db", confirm_ttl_s=300, now=clock)


def save(store, title, author="alice", conversation="c1", content="text"):
    return store.save(author=author, conversation_id=conversation, title=title, content=content)


def test_save_get_and_find(store):
    first = save(store, "Q3 report", content="Diesel grew 40%.")
    save(store, "Top customers", conversation="c2")

    assert store.get(author="alice", report_id=first.id).content == "Diesel grew 40%."
    assert [r.title for r in store.find(author="alice")] == ["Q3 report", "Top customers"]
    assert [r.title for r in store.find(author="alice", conversation_id="c2")] == ["Top customers"]


def test_search_looks_at_title_and_text_in_any_case(store):
    save(store, "Q3 report", content="DIESEL grew 40%.")
    save(store, "Diesel deep dive")
    save(store, "España customers")

    assert len(store.find(author="alice", search="diesel")) == 2
    assert len(store.find(author="alice", search="ESPAÑA")) == 1


def test_only_the_author_sees_a_report(store):
    report = save(store, "Alice's report")

    assert store.get(author="bob", report_id=report.id) is None
    assert store.find(author="bob") == []
    assert store.find(author="bob", report_ids=(report.id,)) == []


def test_request_delete_deletes_nothing(store):
    report = save(store, "Q3 report")

    pending = store.request_delete(author="alice", reports=[report])

    assert store.get(author="alice", report_id=report.id) is not None
    assert [r.id for r in pending.reports] == [report.id]


def test_confirmed_delete_removes_exactly_the_previewed_reports(store):
    old = [save(store, "Diesel Q3"), save(store, "Diesel Q2")]
    pending = store.request_delete(
        author="alice", reports=store.find(author="alice", search="diesel")
    )
    # A report that matches the same filter, saved after the preview, must stay.
    newer = save(store, "Diesel Q4")

    outcome = store.resolve_delete(author="alice", action_id=pending.action_id, confirmed=True)

    assert outcome.status == "deleted"
    assert set(outcome.report_ids) == {r.id for r in old}
    assert [r.id for r in store.find(author="alice")] == [newer.id]


def test_cancelled_delete_keeps_everything(store):
    report = save(store, "Q3 report")
    pending = store.request_delete(author="alice", reports=[report])

    outcome = store.resolve_delete(author="alice", action_id=pending.action_id, confirmed=False)

    assert outcome.status == "cancelled"
    assert store.get(author="alice", report_id=report.id) is not None


def test_expired_delete_keeps_everything(store, clock):
    report = save(store, "Q3 report")
    pending = store.request_delete(author="alice", reports=[report])
    clock.advance(301)

    outcome = store.resolve_delete(author="alice", action_id=pending.action_id, confirmed=True)

    assert outcome.status == "expired"
    assert store.get(author="alice", report_id=report.id) is not None


def test_a_pending_delete_works_once_and_only_for_its_author(store):
    report = save(store, "Q3 report")
    pending = store.request_delete(author="alice", reports=[report])

    assert (
        store.resolve_delete(author="bob", action_id=pending.action_id, confirmed=True).status
        == "cancelled"
    )
    assert store.get(author="alice", report_id=report.id) is not None
    assert (
        store.resolve_delete(author="alice", action_id=pending.action_id, confirmed=True).status
        == "deleted"
    )
    assert (
        store.resolve_delete(author="alice", action_id=pending.action_id, confirmed=True).status
        == "cancelled"
    )


def test_audit_log_keeps_metadata_only(store):
    report = save(store, "Secret Diesel plan", content="Very private numbers")
    pending = store.request_delete(author="alice", reports=[report])
    store.resolve_delete(author="alice", action_id=pending.action_id, confirmed=True)

    log = store.audit_log()

    assert [(e["author"], e["action"], e["count"]) for e in log] == [
        ("alice", "delete requested", 1),
        ("alice", "reports deleted", 1),
    ]
    assert all(e["report_ids"] == f"[{report.id}]" for e in log)
    text = str(log)
    assert "Secret" not in text and "private" not in text


def test_the_database_lives_where_the_settings_say(tmp_path, clock):
    ReportStore(tmp_path / "new" / "folder" / "reports.db", now=clock)

    assert (tmp_path / "new" / "folder" / "reports.db").exists()


@pytest.mark.parametrize(("count", "word"), [(1, "yes"), (3, "yes"), (4, "4"), (12, "12")])
def test_confirmation_word(count, word):
    assert confirmation_word(count, count_above=3) == word
