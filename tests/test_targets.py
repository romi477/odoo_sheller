"""The cards a person writes down: where a remote instance is, kept in a file.

Only ever pointed at a temporary path here — the real one is
`~/.odoo-sheller/targets.json`, beside the admin key, and a test has no
business in that directory.
"""

import json
import os
import stat

import pytest

from odoo_sheller.targets import TargetsError, TargetStore


@pytest.fixture
def store(tmp_path):
    return TargetStore(tmp_path / "targets.json")


def on_disk(store):
    return json.loads(store.path.read_text(encoding="utf-8"))


def test_a_store_that_was_never_written_is_empty_and_stays_unwritten(store):
    assert store.list() == []
    assert not store.path.exists()


def test_an_odoosh_card_is_named_by_its_build(store):
    card = store.add_odoosh("36887345", "build-36887345.dev.odoo.com")
    assert card == {
        "id": "odoosh-36887345",
        "kind": "odoosh",
        "name": "36887345",
        "build": "36887345",
        "host": "build-36887345.dev.odoo.com",
    }
    assert store.get("odoosh-36887345") == card
    assert store.list() == [card]


def test_the_file_has_a_version_and_one_key_per_kind(store):
    store.add_odoosh("1", "h.example.com")
    assert on_disk(store) == {
        "version": 1,
        "odoosh": [{"build": "1", "host": "h.example.com"}],
        "ssh": [],
    }


def test_the_file_is_private_from_the_first_byte(store):
    """It holds no secrets — but it names hosts, and the admin key's directory
    is not somewhere to leave a world-readable file."""
    store.add_odoosh("1", "h.example.com")
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_the_newest_card_comes_first(store):
    store.add_odoosh("1", "a.example.com")
    store.add_odoosh("2", "b.example.com")
    assert [card["build"] for card in store.list()] == ["2", "1"]


def test_adding_a_build_again_updates_it_and_moves_it_first(store):
    store.add_odoosh("1", "a.example.com")
    store.add_odoosh("2", "b.example.com")
    store.add_odoosh("1", "c.example.com")
    assert [(card["build"], card["host"]) for card in store.list()] == [
        ("1", "c.example.com"),
        ("2", "b.example.com"),
    ]


@pytest.mark.parametrize(
    ("build", "host"),
    [
        ("-oProxyCommand=touch /tmp/x", "h.example.com"),
        ("1", "-oProxyCommand=x"),
        ("a b", "h"),
        ("1", "h;id"),
        ("", "h"),
        ("1", ""),
    ],
)
def test_a_card_that_would_not_survive_ssh_is_never_written(store, build, host):
    with pytest.raises(ValueError):
        store.add_odoosh(build, host)
    assert not store.path.exists()


def test_a_card_is_found_by_its_id_and_not_otherwise(store):
    store.add_odoosh("1", "a.example.com")
    with pytest.raises(KeyError):
        store.get("odoosh-2")
    with pytest.raises(KeyError):
        store.get("1")
    with pytest.raises(KeyError):
        store.get("ssh-1")


def test_a_host_can_be_changed_but_not_a_build(store):
    store.add_odoosh("1", "a.example.com")
    assert store.update("odoosh-1", {"host": "b.example.com"})["host"] == "b.example.com"
    assert store.get("odoosh-1")["host"] == "b.example.com"
    with pytest.raises(ValueError, match="build"):
        store.update("odoosh-1", {"build": "2"})
    with pytest.raises(ValueError):
        store.update("odoosh-1", {"host": "-oProxyCommand=x"})
    assert store.get("odoosh-1")["host"] == "b.example.com"
    with pytest.raises(KeyError):
        store.update("odoosh-2", {"host": "c.example.com"})


def test_a_deleted_card_is_gone(store):
    store.add_odoosh("1", "a.example.com")
    store.delete("odoosh-1")
    assert store.list() == []
    assert on_disk(store)["odoosh"] == []
    with pytest.raises(KeyError):
        store.delete("odoosh-1")


def test_a_second_store_on_the_same_file_sees_the_cards(tmp_path):
    TargetStore(tmp_path / "t.json").add_odoosh("1", "a.example.com")
    assert [card["build"] for card in TargetStore(tmp_path / "t.json").list()] == ["1"]


def test_what_a_newer_daemon_wrote_survives_a_write_by_this_one(store):
    """The `ssh` key and anything beside it belong to a version that knows
    more. Rewriting the file must not take them with it."""
    store.path.write_text(
        json.dumps({
            "version": 1,
            "odoosh": [],
            "ssh": [{"id": "ssh-ab12", "name": "prod", "access": "ssh a@b", "launch": "/x shell"}],
            "future": {"kept": True},
        }),
        encoding="utf-8",
    )
    store.add_odoosh("1", "a.example.com")
    written = on_disk(store)
    assert written["ssh"][0]["id"] == "ssh-ab12"
    assert written["future"] == {"kept": True}
    assert written["odoosh"] == [{"build": "1", "host": "a.example.com"}]


def test_a_file_that_cannot_be_read_is_reported_and_left_alone(store):
    """Overwriting it would turn a typo in a hand edit into lost cards."""
    store.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(TargetsError, match="targets.json"):
        store.list()
    with pytest.raises(TargetsError):
        store.add_odoosh("1", "a.example.com")
    assert store.path.read_text(encoding="utf-8") == "{not json"


@pytest.mark.parametrize("content", ["[]", '"x"', '{"version": 2, "odoosh": []}', '{"odoosh": []}', '{"version": 1, "odoosh": {}}'])
def test_a_file_of_another_shape_is_reported_and_left_alone(store, content):
    store.path.write_text(content, encoding="utf-8")
    with pytest.raises(TargetsError):
        store.list()
    assert store.path.read_text(encoding="utf-8") == content


def test_a_write_that_fails_leaves_the_old_file_and_no_litter(store, monkeypatch):
    store.add_odoosh("1", "a.example.com")
    before = store.path.read_text(encoding="utf-8")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("odoo_sheller.targets.os.replace", fail)
    with pytest.raises(OSError, match="disk full"):
        store.add_odoosh("2", "b.example.com")
    assert store.path.read_text(encoding="utf-8") == before
    assert sorted(path.name for path in store.path.parent.iterdir()) == ["targets.json"]
