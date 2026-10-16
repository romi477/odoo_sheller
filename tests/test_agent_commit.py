import json

import pytest

from odoo_sheller.agent_commit import AgentCommitError, AgentCommitStore


@pytest.fixture
def path(tmp_path):

    return tmp_path / "agent-commit.json"


def test_a_missing_file_means_nothing_is_on_and_is_not_created(path):
    store = AgentCommitStore(path)
    assert store.entries() == []
    assert store.is_on("mig18") is False
    assert not path.exists()


def test_turn_on_persists_and_survives_a_new_store(path):
    entry = AgentCommitStore(path).turn_on("mig18")
    assert entry["container"] == "mig18"
    assert entry["since"].endswith("Z")
    assert AgentCommitStore(path).is_on("mig18") is True
    assert AgentCommitStore(path).entries() == [entry]
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_turn_on_twice_keeps_one_entry_and_the_first_since(path):
    store = AgentCommitStore(path)
    first = store.turn_on("mig18")
    data = json.loads(path.read_text())
    data["containers"][0]["since"] = "2026-01-01T00:00:00Z"
    path.write_text(json.dumps(data))
    again = store.turn_on("mig18")
    assert again["since"] == "2026-01-01T00:00:00Z"
    assert [e["container"] for e in store.entries()] == ["mig18"]
    assert first["container"] == again["container"]


def test_entries_are_sorted_by_name(path):
    store = AgentCommitStore(path)
    store.turn_on("mig18")
    store.turn_on("integra19")
    assert [e["container"] for e in store.entries()] == ["integra19", "mig18"]


def test_turn_off_reports_whether_it_was_on(path):
    store = AgentCommitStore(path)
    store.turn_on("mig18")
    assert store.turn_off("mig18") is True
    assert store.is_on("mig18") is False
    assert store.turn_off("mig18") is False


def test_turning_off_with_no_file_creates_none(path):
    assert AgentCommitStore(path).turn_off("mig18") is False
    assert not path.exists()


def test_is_on_none_is_false(path):
    store = AgentCommitStore(path)
    store.turn_on("mig18")
    assert store.is_on(None) is False


@pytest.mark.parametrize("bad", ["", "-x", "a b", "a/b", "x" * 200, "../etc", ".hidden"])
def test_a_name_docker_would_not_give_is_refused(path, bad):
    with pytest.raises(ValueError):
        AgentCommitStore(path).turn_on(bad)
    assert not path.exists()


def test_an_unreadable_file_is_reported_and_left_alone(path):
    path.write_text("{nope")
    store = AgentCommitStore(path)
    with pytest.raises(AgentCommitError):
        store.entries()
    with pytest.raises(AgentCommitError):
        store.is_on("mig18")
    with pytest.raises(AgentCommitError):
        store.turn_on("mig18")
    with pytest.raises(AgentCommitError):
        store.turn_off("mig18")
    assert path.read_text() == "{nope"


@pytest.mark.parametrize(
    "content",
    [
        {"version": 2, "containers": []},
        {"version": 1, "containers": "mig18"},
        {"version": 1, "containers": [{"since": "2026-10-16T00:00:00Z"}]},
        [],
    ],
)
def test_a_file_this_code_cannot_read_is_refused(path, content):
    path.write_text(json.dumps(content))
    with pytest.raises(AgentCommitError):
        AgentCommitStore(path).entries()
