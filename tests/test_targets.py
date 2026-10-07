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
            "ssh": [{
                "id": "ssh-ab12", "name": "prod", "access": "ssh a@b", "launch": "/x shell",
                "database": None, "stage": "production",
            }],
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


def test_the_new_file_is_on_disk_before_it_replaces_the_old_one(store, monkeypatch):
    """`os.replace` makes the swap atomic against a crash of this process. The
    promise "either version" also has to hold when the power goes: a rename that
    reaches the disk before the data does leaves an empty file with the real
    name."""
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(
        "odoo_sheller.targets.os.fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1]
    )
    monkeypatch.setattr(
        "odoo_sheller.targets.os.replace",
        lambda *args: (order.append("replace"), real_replace(*args))[1],
    )
    store.add_odoosh("1", "a.example.com")
    assert order == ["fsync", "replace"]


# --- a server reached by ssh ------------------------------------------------

ACCESS = "ssh -i /k/key.pem ubuntu@srv.example.com sudo -n -u odoo -H"
LAUNCH = "/opt/odoo/env/bin/python /opt/odoo/odoo-bin shell -c /opt/odoo/odoo.conf"


def anything_is_a_file(path):
    return True


@pytest.fixture
def ssh_store(tmp_path):
    return TargetStore(tmp_path / "targets.json", is_file=anything_is_a_file)


def add_ssh(store, name="acme prod", **overrides):
    fields = {"access": ACCESS, "launch": LAUNCH, "stage": "production"}
    fields.update(overrides)

    return store.add_ssh(name, **fields)


def test_an_ssh_card_is_what_the_person_wrote(ssh_store):
    card = add_ssh(ssh_store)
    assert card["id"].startswith("ssh-")
    assert {k: v for k, v in card.items() if k != "id"} == {
        "kind": "ssh", "name": "acme prod", "access": ACCESS, "launch": LAUNCH,
        "stage": "production",
    }
    assert ssh_store.get(card["id"]) == card
    assert on_disk(ssh_store)["ssh"] == [{k: v for k, v in card.items() if k != "kind"}]


def test_what_was_typed_is_stored_not_what_it_was_normalised_to(ssh_store):
    """The person edits this later; `~` is theirs, expanded where it is used."""
    card = add_ssh(ssh_store, access="ssh 1.2.3.4 -i ~/.ssh/k.pem -l ubuntu")
    assert card["access"] == "ssh 1.2.3.4 -i ~/.ssh/k.pem -l ubuntu"


def test_the_default_stage_is_production(ssh_store):
    """Nothing on a plain server says what it is, and a guess would be wrong in
    the direction that matters. A human says otherwise, on the card."""
    card = ssh_store.add_ssh("x", access=ACCESS, launch=LAUNCH)
    assert card["stage"] == "production"
    assert "database" not in card, "the Launch is the one place a database is written"


@pytest.mark.parametrize("stage", ["production", "staging"])
def test_the_stages_a_human_may_declare(ssh_store, stage):
    """Two, because the guard tells two apart: production is written to only by a
    human who types its name, once; everything else is closed until granted."""
    assert add_ssh(ssh_store, stage=stage)["stage"] == stage


def test_the_third_stage_there_used_to_be_is_no_longer_one_to_declare(ssh_store):
    with pytest.raises(ValueError, match="stage"):
        add_ssh(ssh_store, stage="development")


def test_a_card_written_when_there_were_three_stages_reads_as_staging(ssh_store):
    """`development` behaved exactly as `staging` did, so an old card keeps its
    meaning — and a stage the store no longer knows would have made the whole
    file unreadable."""
    ssh_store.path.write_text(
        json.dumps({
            "version": 1, "odoosh": [],
            "ssh": [{
                "id": "ssh-ab12", "name": "dev box", "access": ACCESS, "launch": LAUNCH,
                "database": None, "stage": "development",
            }],
        }),
        encoding="utf-8",
    )
    assert ssh_store.list()[0]["stage"] == "staging"
    ssh_store.add_odoosh("1", "a.example.com")
    assert on_disk(ssh_store)["ssh"][0]["stage"] == "staging"


@pytest.mark.parametrize("stage", ["", "prod", "PRODUCTION", "test", None, 1])
def test_a_stage_that_is_not_one_of_them_is_refused(ssh_store, stage):
    with pytest.raises(ValueError, match="stage"):
        add_ssh(ssh_store, stage=stage)
    assert ssh_store.list() == []


@pytest.mark.parametrize(
    "name", ["", "   ", "a\nb", "a\tb", "x" * 61, "\x00", "a\x1bb"]
)
def test_a_name_that_cannot_be_shown_is_refused(ssh_store, name):
    with pytest.raises(ValueError, match="name"):
        add_ssh(ssh_store, name=name)


def test_the_name_is_trimmed_and_unique_among_ssh_cards(ssh_store):
    assert add_ssh(ssh_store, name="  acme  ")["name"] == "acme"
    with pytest.raises(ValueError, match="already"):
        add_ssh(ssh_store, name="ACME")
    assert len(ssh_store.list()) == 1


def test_a_recipe_the_grammar_refuses_is_never_written(ssh_store):
    for fields in (
        {"access": "ssh -o ProxyCommand=x u@h"},
        {"access": "ssh u@h sudo su"},
        {"launch": "odoo-bin shell"},
    ):
        with pytest.raises(ValueError):
            add_ssh(ssh_store, **fields)
    assert not ssh_store.path.exists()


def test_a_named_key_that_is_not_there_is_refused(tmp_path):
    store = TargetStore(tmp_path / "t.json", is_file=lambda path: False)
    with pytest.raises(ValueError, match="key.pem"):
        store.add_ssh("x", access=ACCESS, launch=LAUNCH)


def test_the_kinds_are_listed_together_each_newest_first(ssh_store):
    ssh_store.add_odoosh("1", "a.example.com")
    first = add_ssh(ssh_store, name="one")
    second = add_ssh(ssh_store, name="two")
    ssh_store.add_odoosh("2", "b.example.com")
    assert [card["id"] for card in ssh_store.list()] == [
        "odoosh-2", "odoosh-1", second["id"], first["id"],
    ]
    assert on_disk(ssh_store)["version"] == 1


def test_an_ssh_card_is_changed_field_by_field_and_rechecked_whole(ssh_store):
    card = add_ssh(ssh_store)
    changed = ssh_store.update(card["id"], {"stage": "staging", "name": "renamed"})
    assert (changed["stage"], changed["name"]) == ("staging", "renamed")
    assert changed["access"] == ACCESS
    with pytest.raises(ValueError):
        ssh_store.update(card["id"], {"access": "ssh -o ProxyCommand=x u@h"})
    assert ssh_store.get(card["id"]) == changed, "a refused change changes nothing"


def test_changing_a_name_to_another_cards_is_refused(ssh_store):
    add_ssh(ssh_store, name="one")
    two = add_ssh(ssh_store, name="two")
    with pytest.raises(ValueError, match="already"):
        ssh_store.update(two["id"], {"name": "One"})
    assert ssh_store.update(two["id"], {"name": "two"})["name"] == "two", "its own name is not a clash"


def test_a_field_an_ssh_card_does_not_have_is_refused(ssh_store):
    card = add_ssh(ssh_store)
    with pytest.raises(ValueError, match="build"):
        ssh_store.update(card["id"], {"build": "1"})


def test_an_ssh_card_is_deleted_and_only_that_one(ssh_store):
    ssh_store.add_odoosh("1", "a.example.com")
    card = add_ssh(ssh_store)
    ssh_store.delete(card["id"])
    assert [c["id"] for c in ssh_store.list()] == ["odoosh-1"]
    with pytest.raises(KeyError):
        ssh_store.delete(card["id"])
    with pytest.raises(KeyError):
        ssh_store.get("ssh-nope")


def test_an_odoosh_card_does_not_answer_to_an_ssh_id(ssh_store):
    ssh_store.add_odoosh("1", "a.example.com")
    with pytest.raises(KeyError):
        ssh_store.get("ssh-1")


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "ssh-1", "name": "n", "access": "a", "launch": "l", "database": None, "stage": 3},
        {"id": 1, "name": "n", "access": "a", "launch": "l", "database": None, "stage": "production"},
        "text",
    ],
)
def test_an_ssh_entry_of_another_shape_is_reported_and_left_alone(ssh_store, entry):
    ssh_store.path.write_text(
        json.dumps({"version": 1, "odoosh": [], "ssh": [entry]}), encoding="utf-8"
    )
    before = ssh_store.path.read_text(encoding="utf-8")
    with pytest.raises(TargetsError):
        ssh_store.list()
    assert ssh_store.path.read_text(encoding="utf-8") == before


def test_two_ssh_cards_never_share_an_id(ssh_store):
    ids = {add_ssh(ssh_store, name=f"n{i}")["id"] for i in range(30)}
    assert len(ids) == 30


def test_the_recipe_of_a_card_is_parsed_where_it_is_used(ssh_store):
    card = add_ssh(ssh_store, launch=LAUNCH + " -d acme")
    access, launch = ssh_store.recipe(card["id"])
    assert access.destination == "ubuntu@srv.example.com"
    assert launch.argv[-2:] == ("-d", "acme")
    assert launch.database == "acme"
    ssh_store.add_odoosh("1", "h")
    with pytest.raises(KeyError):
        ssh_store.recipe("odoosh-1")


def test_a_card_has_no_database_field_to_fill(ssh_store):
    with pytest.raises(TypeError):
        add_ssh(ssh_store, database="acme")
    card = add_ssh(ssh_store, name="other")
    with pytest.raises(ValueError, match="database"):
        ssh_store.update(card["id"], {"database": "acme"})


def write_old_card(store, **entry):
    store.path.write_text(
        json.dumps({"version": 1, "odoosh": [], "ssh": [{
            "id": "ssh-1", "name": "n", "access": ACCESS, "launch": LAUNCH,
            "stage": "staging", **entry,
        }]}),
        encoding="utf-8",
    )


def test_a_card_that_had_a_database_field_keeps_its_database_in_the_launch(ssh_store):
    """The field is gone, and the card must still open the database it opened.
    Moving it into the Launch as `-d` is the same thing said in the one place
    that is left."""
    write_old_card(ssh_store, database="acme")
    card = ssh_store.get("ssh-1")
    assert card["launch"] == LAUNCH + " -d acme"
    assert "database" not in card
    assert ssh_store.recipe("ssh-1")[1].database == "acme"
    ssh_store.add_odoosh("1", "a.example.com")
    assert "database" not in on_disk(ssh_store)["ssh"][0]


def test_a_database_that_needs_quoting_is_quoted_on_its_way_into_the_launch(ssh_store):
    write_old_card(ssh_store, database="my db")
    assert ssh_store.get("ssh-1")["launch"] == LAUNCH + " -d 'my db'"
    assert ssh_store.recipe("ssh-1")[1].database == "my db"


def test_an_old_cards_database_does_not_override_one_the_launch_already_has(ssh_store):
    """The Launch is what ran. A card that carried both was refused when it was
    written, so this is only a hand-edited file — and what ran wins."""
    write_old_card(ssh_store, launch=LAUNCH + " -d from-launch", database="from-field")
    assert ssh_store.get("ssh-1")["launch"] == LAUNCH + " -d from-launch"


def test_a_card_that_never_had_a_database_reads_the_same(ssh_store):
    write_old_card(ssh_store)
    assert ssh_store.get("ssh-1")["launch"] == LAUNCH
    assert "database" not in ssh_store.get("ssh-1")
