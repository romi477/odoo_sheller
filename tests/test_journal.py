import json
from datetime import UTC, datetime

import pytest

from odoo_sheller import journal


def test_write_appends_one_json_object_per_line(tmp_path):
    path = tmp_path / "s.jsonl"
    log = journal.Journal(path)
    log.write("session_open", container="c", database="db")
    log.write("exec", id=1, code="print('x')")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["kind"] == "session_open"
    assert first["container"] == "c"
    assert "ts" in first


def test_records_reads_back_what_was_written(tmp_path):
    log = journal.Journal(tmp_path / "s.jsonl")
    log.write("exec", id=1, code="a")
    log.write("result", id=1, stdout="out")
    kinds = [record["kind"] for record in log.records()]
    assert kinds == ["exec", "result"]


def test_full_output_is_stored_untruncated(tmp_path):
    log = journal.Journal(tmp_path / "s.jsonl")
    log.write("result", id=1, stdout="x" * 500_000)
    assert len(log.records()[0]["stdout"]) == 500_000


def test_journal_path_is_sortable_and_names_the_target(tmp_path):
    path = journal.journal_path(
        tmp_path, "abc123", "integra19", "integra_db_19", datetime(2026, 8, 15, 14, 30, 5, tzinfo=UTC)
    )
    assert path.parent == tmp_path
    assert path.name.startswith("2026-08-15T14-30-05")
    assert "integra19" in path.name
    assert "integra_db_19" in path.name
    assert path.name.endswith("-abc123.jsonl")


def test_list_journals_summarises_each_file(tmp_path):
    log = journal.Journal(journal.journal_path(
        tmp_path, "abc123", "c", "db", datetime(2026, 8, 15, 14, 30, 5, tzinfo=UTC)
    ))
    log.write("session_open", container="c", database="db", odoo="19.0")
    log.write("exec", id=1, code="a")
    log.write("result", id=1, stdout="", error=None)
    log.write("commit", id=2)

    entries = journal.list_journals(tmp_path)
    assert len(entries) == 1
    assert entries[0]["session_id"] == "abc123"
    assert entries[0]["container"] == "c"
    assert entries[0]["database"] == "db"
    assert entries[0]["commands"] == 1
    assert entries[0]["committed"] is True
    assert entries[0]["duration"] is not None
    assert entries[0]["duration"] >= 0


def test_list_journals_duration_is_seconds_between_first_and_last_record(tmp_path):
    path = journal.journal_path(
        tmp_path, "abc123", "c", "db", datetime(2026, 8, 15, 14, 30, 5, tzinfo=UTC)
    )
    records = [
        {"ts": "2026-08-15T14:30:05+00:00", "kind": "session_open",
         "container": "c", "database": "db", "odoo": "19.0"},
        {"ts": "2026-08-15T14:30:15+00:00", "kind": "exec", "id": 1, "code": "a"},
        {"ts": "2026-08-15T14:32:25+00:00", "kind": "session_close"},
    ]
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")

    entries = journal.list_journals(tmp_path)
    assert entries[0]["duration"] == 140.0


def test_list_journals_duration_is_null_for_empty_file(tmp_path):
    path = journal.journal_path(
        tmp_path, "empty1", "c", "db", datetime(2026, 8, 15, 14, 30, 5, tzinfo=UTC)
    )
    path.write_text("", encoding="utf-8")

    entries = journal.list_journals(tmp_path)
    assert entries[0]["duration"] is None


def test_delete_journal_unlinks_the_file(tmp_path):
    path = journal.journal_path(
        tmp_path, "abc123", "c", "db", datetime(2026, 8, 15, 14, 30, 5, tzinfo=UTC)
    )
    journal.Journal(path).write("session_open", container="c", database="db")
    assert path.exists()

    deleted = journal.delete_journal(tmp_path, "abc123")

    assert deleted == path
    assert not path.exists()


def test_delete_journal_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        journal.delete_journal(tmp_path, "nope")


@pytest.mark.parametrize("pattern", ["*", "?bc123", "[a-z]*", "*123"])
def test_delete_journal_never_treats_the_id_as_a_pattern(tmp_path, pattern):
    """A glob metacharacter in the URL must not reach an unrelated journal."""
    kept = [
        journal.journal_path(
            tmp_path, session_id, "c", "db", datetime(2026, 8, 15, 14, 30, 5, tzinfo=UTC)
        )
        for session_id in ("abc123", "def456")
    ]
    for path in kept:
        journal.Journal(path).write("session_open", container="c", database="db")

    with pytest.raises(FileNotFoundError):
        journal.delete_journal(tmp_path, pattern)

    assert all(path.exists() for path in kept)


def test_delete_journal_matches_the_id_the_listing_reports(tmp_path):
    """delete and list must derive the id the same way or one of them is wrong."""
    for session_id in ("abc123", "def456"):
        path = journal.journal_path(
            tmp_path, session_id, "c-1", "db_2", datetime(2026, 8, 15, 14, 30, 5, tzinfo=UTC)
        )
        journal.Journal(path).write("session_open", container="c-1", database="db_2")
    listed = {entry["session_id"] for entry in journal.list_journals(tmp_path)}
    assert listed == {"abc123", "def456"}

    journal.delete_journal(tmp_path, "abc123")

    assert {entry["session_id"] for entry in journal.list_journals(tmp_path)} == {"def456"}


def test_to_markdown_renders_a_readable_transcript(tmp_path):
    records = [
        {"kind": "session_open", "ts": "2026-08-15T14:30:05", "container": "c",
         "database": "db", "odoo": "19.0"},
        {"kind": "exec", "ts": "2026-08-15T14:30:10", "id": 1, "code": "print('x')"},
        {"kind": "result", "ts": "2026-08-15T14:30:11", "id": 1, "stdout": "x\n",
         "result": None, "error": None, "duration": 0.5},
        {"kind": "commit", "ts": "2026-08-15T14:30:20", "id": 2},
    ]
    text = journal.to_markdown(records)
    assert text.startswith("# Session on c / db")
    assert "```python\nprint('x')\n```" in text
    assert "x\n" in text
    assert "commit" in text.lower()


def test_to_markdown_numbers_commands_not_frame_ids():
    """Commit and close share the request-id counter; headings must not skip."""
    records = [
        {"kind": "exec", "ts": "t1", "id": 1, "code": "a()"},
        {"kind": "result", "ts": "t2", "id": 1, "stdout": "a\n", "duration": 0.1},
        {"kind": "commit", "ts": "t3", "id": 2},
        {"kind": "exec", "ts": "t4", "id": 3, "code": "b()"},
        {"kind": "timeout", "ts": "t5", "id": 3, "seconds": 30},
        {"kind": "abandoned_result", "ts": "t6", "id": 3, "stdout": "late\n", "duration": 31},
    ]
    text = journal.to_markdown(records)
    assert journal.session_meta(records)["commands"] == 2
    assert "## Command 1 " in text
    assert "## Command 2 " in text
    assert "## Command 3" not in text
    assert "Command 2 exceeded 30s" in text
    assert "Late result of command 2" in text


def test_feed_from_records_pairs_exec_with_result():
    feed = journal.feed_from_records([
        {"kind": "session_open", "ts": "t0", "container": "c", "database": "db"},
        {"kind": "exec", "ts": "t1", "id": 1, "code": "print(1)"},
        {"kind": "result", "ts": "t2", "t": "result", "id": 1, "stdout": "1\n",
         "stdout_truncated": False, "result": "None", "result_truncated": False,
         "error": None, "duration": 0.04},
        {"kind": "stderr", "ts": "t3", "line": "INFO something"},
    ])
    assert feed["history"] == ["print(1)"]
    assert feed["entries"] == [{
        "kind": "exec",
        "id": 1,
        "ordinal": 1,
        "code": "print(1)",
        "status": "done",
        "actor": None,
        "result": {
            "id": 1,
            "stdout": "1\n",
            "stdout_truncated": False,
            "result": "None",
            "result_truncated": False,
            "error": None,
            "duration": 0.04,
        },
    }]
    assert "logs" not in feed


def test_feed_from_records_inserts_successful_commit_and_skips_failed_boundary():
    feed = journal.feed_from_records([
        {"kind": "exec", "id": 1, "code": "a"},
        {"kind": "result", "id": 1, "stdout": "", "error": None, "duration": 0.1},
        {"kind": "commit", "id": 2, "error": None},
        {"kind": "rollback", "id": 3, "error": {"type": "Error", "message": "nope"}},
        {"kind": "exec", "id": 4, "code": "b"},
        {"kind": "result", "id": 4, "stdout": "", "error": None, "duration": 0.1},
    ])
    kinds = [entry["kind"] for entry in feed["entries"]]
    assert kinds == ["exec", "commit", "exec"]
    assert feed["history"] == ["a", "b"]
    assert [e["ordinal"] for e in feed["entries"] if e["kind"] == "exec"] == [1, 2]


def test_feed_from_records_timeout_then_abandoned_result_replaces_payload():
    feed = journal.feed_from_records([
        {"kind": "exec", "id": 7, "code": "sleep()"},
        {"kind": "timeout", "id": 7, "seconds": 300},
        {"kind": "abandoned_result", "id": 7, "stdout": "late\n",
         "error": None, "duration": 301.0, "ts": "t9"},
    ])
    entry = feed["entries"][0]
    assert entry["status"] == "done"
    assert entry["result"]["stdout"] == "late\n"
    assert entry["result"]["duration"] == 301.0
    assert "ts" not in entry["result"]
    assert "kind" not in entry["result"]


def test_feed_from_records_trailing_exec_stays_running():
    feed = journal.feed_from_records([
        {"kind": "interrupt", "ts": "t0"},
        {"kind": "exec", "id": 1, "code": "still going"},
    ])
    assert feed["entries"][0]["status"] == "running"
    assert feed["entries"][0]["result"] is None


def test_feed_from_records_timeout_without_result_matches_live_504_cell():
    feed = journal.feed_from_records([
        {"kind": "exec", "id": 3, "code": "SLEEP"},
        {"kind": "timeout", "id": 3, "seconds": 300},
    ])
    entry = feed["entries"][0]
    assert entry["status"] == "error"
    assert entry["result"]["error"] == {
        "type": "TimeoutError",
        "message": "Command exceeded its ceiling and was interrupted.",
        "traceback": "",
    }


def test_feed_from_records_pairs_run_test_with_result():
    feed = journal.feed_from_records([
        {"kind": "run_test", "ts": "t1", "id": 1, "module": "sale",
         "test_class": "TestSaleOrder", "test_method": "test_x", "actor": None},
        {"kind": "result", "ts": "t2", "id": 1, "stdout": "", "error": None,
         "duration": 1.2, "test": {"tests_run": 1, "failures": 0, "errors": 0,
         "skipped": 0, "success": True}, "stderr": [], "discarded_pending": False},
    ])
    assert feed["history"] == []  # not code, must not pollute editor recall
    assert len(feed["entries"]) == 1
    entry = feed["entries"][0]
    assert entry["kind"] == "run_test"
    assert entry["module"] == "sale"
    assert entry["test_class"] == "TestSaleOrder"
    assert entry["test_method"] == "test_x"
    assert entry["status"] == "done"
    assert entry["result"]["test"]["success"] is True


def test_feed_from_records_numbers_exec_and_run_test_on_one_counter():
    """Two counters drift: run_test counted entries (which commit also grows),
    exec counted history (which run_test never grows), so numbers collided."""
    feed = journal.feed_from_records([
        {"kind": "exec", "id": 1, "code": "a"},
        {"kind": "result", "id": 1, "stdout": "", "error": None, "duration": 0.1},
        {"kind": "run_test", "id": 2, "module": "m", "test_class": "T"},
        {"kind": "result", "id": 2, "stdout": "", "error": None, "duration": 0.1},
        {"kind": "exec", "id": 3, "code": "b"},
        {"kind": "result", "id": 3, "stdout": "", "error": None, "duration": 0.1},
    ])
    assert [e["ordinal"] for e in feed["entries"]] == [1, 2, 3]


def test_feed_ordinals_ignore_entries_that_are_not_commands():
    """A commit between two commands must not push the next number along."""
    feed = journal.feed_from_records([
        {"kind": "exec", "id": 1, "code": "a"},
        {"kind": "result", "id": 1, "error": None},
        {"kind": "commit", "id": 2, "error": None},
        {"kind": "run_test", "id": 3, "module": "m", "test_class": "T"},
        {"kind": "result", "id": 3, "error": None},
    ])
    numbered = [e["ordinal"] for e in feed["entries"] if "ordinal" in e]
    assert numbered == [1, 2]


def test_feed_and_markdown_agree_on_command_numbers():
    """The transcript and the rebuilt feed must not disagree about the same run."""
    records = [
        {"kind": "exec", "ts": "t1", "id": 1, "code": "a"},
        {"kind": "result", "ts": "t2", "id": 1, "error": None, "duration": 0.1},
        {"kind": "commit", "ts": "t3", "id": 2, "error": None},
        {"kind": "run_test", "ts": "t4", "id": 3, "module": "m", "test_class": "T"},
        {"kind": "result", "ts": "t5", "id": 3, "error": None, "duration": 0.1},
    ]
    text = journal.to_markdown(records)
    feed = journal.feed_from_records(records)
    run_test_entry = next(e for e in feed["entries"] if e["kind"] == "run_test")
    assert f"## Test {run_test_entry['ordinal']} " in text


def test_feed_from_records_run_test_timeout_reads_as_still_running():
    feed = journal.feed_from_records([
        {"kind": "run_test", "id": 5, "module": "sale", "test_class": "Slow"},
        {"kind": "timeout", "id": 5, "seconds": 300},
    ])
    entry = feed["entries"][0]
    assert entry["kind"] == "run_test"
    assert entry["status"] == "error"
    assert entry["result"]["error"]["type"] == "TimeoutError"


def test_session_meta_counts_run_test_as_a_command():
    records = [
        {"kind": "session_open", "ts": "t0", "container": "c", "database": "db"},
        {"kind": "run_test", "ts": "t1", "id": 1, "module": "sale", "test_class": "T"},
        {"kind": "result", "ts": "t2", "id": 1, "error": None},
    ]
    assert journal.session_meta(records)["commands"] == 1


def test_to_markdown_renders_a_run_test_heading_and_summary():
    records = [
        {"kind": "run_test", "ts": "t1", "id": 1, "module": "sale",
         "test_class": "TestSaleOrder", "test_method": "test_x"},
        {"kind": "result", "ts": "t2", "id": 1, "stdout": "", "error": None,
         "duration": 1.2, "test": {"tests_run": 1, "failures": 0, "errors": 0,
         "skipped": 0, "success": True}},
    ]
    text = journal.to_markdown(records)
    assert "sale.TestSaleOrder.test_x" in text
    assert "1 run" in text
    assert "PASS" in text


def test_feed_from_records_ignores_unmatched_result():
    feed = journal.feed_from_records([
        {"kind": "result", "id": 99, "stdout": "orphan"},
        {"kind": "timeout", "id": 99},
    ])
    assert feed == {"history": [], "entries": []}


def test_feed_from_records_include_logs_returns_stderr_with_timestamps():
    feed = journal.feed_from_records(
        [
            {"kind": "exec", "id": 1, "code": "a"},
            {"kind": "stderr", "ts": "t1", "line": "INFO a"},
            {"kind": "result", "id": 1, "error": None},
            {"kind": "stderr", "ts": "t2", "line": "ERROR b"},
        ],
        include_logs=True,
    )
    assert [entry["kind"] for entry in feed["entries"]] == ["exec"]
    assert feed["logs"] == [
        {"ts": "t1", "line": "INFO a"},
        {"ts": "t2", "line": "ERROR b"},
    ]


def test_feed_from_records_include_logs_is_empty_list_when_journal_has_no_stderr():
    feed = journal.feed_from_records(
        [{"kind": "exec", "id": 1, "code": "a"}],
        include_logs=True,
    )
    assert feed["logs"] == []


def test_feed_from_records_caps_logs_to_the_tail():
    records = [{"kind": "stderr", "ts": f"t{i}", "line": f"line {i}"} for i in range(50)]
    feed = journal.feed_from_records(records, include_logs=True, log_tail=10)
    assert len(feed["logs"]) == 10
    assert feed["logs"][0]["line"] == "line 40"
    assert feed["logs_truncated"] is True


def test_feed_from_records_keeps_every_log_when_under_the_cap():
    records = [{"kind": "stderr", "ts": "t0", "line": "only one"}]
    feed = journal.feed_from_records(records, include_logs=True, log_tail=10)
    assert feed["logs_truncated"] is False


def test_feed_from_records_marks_a_late_result_as_abandoned():
    feed = journal.feed_from_records([
        {"kind": "exec", "id": 7, "code": "sleep()"},
        {"kind": "timeout", "id": 7, "seconds": 300},
        {"kind": "abandoned_result", "id": 7, "stdout": "late\n",
         "error": None, "duration": 301.0},
    ])
    entry = feed["entries"][0]
    assert entry["status"] == "done"
    assert entry["abandoned"] is True, "a late result must not read as an ordinary success"
    assert entry["timed_out"] is True


def test_markdown_keeps_the_odoo_log_it_used_to_drop():
    """Markdown is the default export, and it silently dropped every stderr
    record — which is what convinced an agent no log existed at all, and what
    swallowed logged tracebacks that are not an exception result."""
    records = [
        {"kind": "exec", "id": 1, "code": "boom()", "ts": "12:00:00"},
        {"kind": "stderr", "line": "INFO odoo: starting", "ts": "12:00:01"},
        {"kind": "stderr", "line": "ERROR odoo: Traceback (most recent call last):",
         "ts": "12:00:02"},
        {"kind": "stderr", "line": '  File "x.py", line 1, in boom', "ts": "12:00:02"},
        {"kind": "result", "id": 1, "stdout": "", "result": None, "error": None,
         "duration": 0.1, "ts": "12:00:03"},
    ]
    text = journal.to_markdown(records)
    assert "INFO odoo: starting" in text
    assert "Traceback (most recent call last):" in text
    assert 'File "x.py", line 1, in boom' in text
    # One block per run of consecutive lines, labelled for what it is.
    assert text.count("Odoo log") == 1


def test_markdown_labels_the_log_between_two_commands_separately():
    records = [
        {"kind": "exec", "id": 1, "code": "a()", "ts": "12:00:00"},
        {"kind": "stderr", "line": "first", "ts": "12:00:01"},
        {"kind": "result", "id": 1, "stdout": "", "result": None, "error": None,
         "duration": 0.1, "ts": "12:00:02"},
        {"kind": "exec", "id": 2, "code": "b()", "ts": "12:00:03"},
        {"kind": "stderr", "line": "second", "ts": "12:00:04"},
        {"kind": "result", "id": 2, "stdout": "", "result": None, "error": None,
         "duration": 0.1, "ts": "12:00:05"},
    ]
    text = journal.to_markdown(records)
    assert text.count("Odoo log") == 2
    assert text.index("first") < text.index("## Command 2")
