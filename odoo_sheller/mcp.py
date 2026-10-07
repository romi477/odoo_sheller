"""MCP server: the same HTTP API, exposed as tools for an agent.

Runs over stdio, which carries JSON-RPC — so nothing here may write to stdout.
Use `logging` (it writes to stderr) and never `print`.

This module holds no session logic. Every tool is one call to the daemon, which
must already be running: Claude Desktop restarts its MCP servers freely, and a
daemon started as a child of this process would take every live session down
with it on each restart.
"""

import ast
import asyncio
import json
import logging
import os
import secrets
from typing import Any

import httpx2 as httpx
from mcp.server import MCPServer
from mcp_types import ToolAnnotations

logger = logging.getLogger(__name__)

def daemon_url() -> str:
    """Where the daemon is.

    ODOO_SHELLER_URL wins when set: it is the only one of the two that can name
    a host. Otherwise follow ODOO_SHELLER_PORT, because in the container image
    that variable is what moves the listener — and this server usually runs
    inside that same container, started by `docker exec`. The two used to
    disagree in silence, and the first sign of it was every tool answering
    daemon_unreachable about a daemon that was running two lines away.
    """
    explicit = os.environ.get("ODOO_SHELLER_URL")
    if explicit:

        return explicit

    return f"http://127.0.0.1:{os.environ.get('ODOO_SHELLER_PORT', '8765')}"


DAEMON_URL = daemon_url()
AGENT_LABEL = "mcp-agent"
# How long a call waits for the daemon to answer. MCP hosts cut a tool call off
# at around a minute. This is our patience only: the daemon's own ceiling for
# a command stays five minutes, and a command that outlasts this wait keeps
# running — see `os_exec`.
CALL_WAIT = 40.0
# os_run_test's ceiling when none is passed, by what the spec names: one test
# is usually seconds, a class minutes, a module tens of minutes.
DEFAULT_TEST_TIMEOUTS = {"method": 30.0, "class": 300.0, "module": 1800.0}
# `Session.start` waits this long for the bootstrap's hello. Giving up on the
# open call any earlier strands a session the daemon then goes on to register,
# whose write key was only ever in the response we stopped waiting for.
SESSION_START_CEILING = 90.0
MAX_TEST_TIMEOUT = 3600.0  # matches the API's own ceiling on RunTestBody
# How long one tool call may block. MCP hosts cut a call off at around a
# minute, and nothing we set on our side changes that — so a long run has to
# be handed back as "still going" rather than held onto until the host kills
# it. Override for a host with a different patience.
MCP_CALL_BUDGET = float(os.environ.get("ODOO_SHELLER_MCP_BUDGET", "40"))
# The floor for the run leg. If opening the session already ate the budget we
# still have to send the request — otherwise nothing runs at all and the
# session id we hand back points at an idle session.
RUN_START_GRACE = 5.0
TEST_RESULT_POLL = 2.0  # how often os_test_result looks while it waits
# Hosts deliver only the first 2048 characters of a server's instructions —
# measured on two of them, cut mid-sentence at exactly this offset. Everything
# past it never reaches the model, so INSTRUCTIONS has to fit and the rest
# lives in HELP, behind os_help.
INSTRUCTION_CAP = 2048
MAX_STDOUT = 4000
# A source read is what an agent would otherwise get with `sed`, so the budget
# is larger than a command's output — but still a budget: a whole file is
# rarely the question, and a range is one argument away.
MAX_SOURCE = 8000
# A module listing is counted in files, not characters: a truncated path is a
# path that leads nowhere.
MAX_LISTING = 400
MAX_RESULT = 2000
# A transcript page. A module run's journal is near a megabyte and the largest
# reach a hundred; a whole one in an answer is context nothing else can use.
MAX_JOURNAL = 20000

INSTRUCTIONS = """\
odoo-sheller runs Python in a live Odoo shell. `env` and `self` are Odoo's
own, and variables persist between commands: a session is a workspace, not a
series of scripts.

Hosts deliver only the first 2048 characters of this. The rest is behind
os_help(topic), one cheap call:

  sessions       opening, closing, death, one at a time
  ownership      handover, whose session it is
  commit         the grant ritual
  remote_server  a remote Odoo, and production
  limits         what never to attempt
  orm            idioms instead of Python loops
  code           which override actually runs
  log            what Odoo logged
  records        reading a record whole
  modules        installing, upgrading, migrations
  jobs           with_delay, run inline
  tests          a test, a class or a whole module
  watching       the human watches

Read the topic before working around something. These rules survive
truncation and are not negotiable:

- One command at a time per session; a second is `session_busy`, never
  queued. Wait, or stop it with os_interrupt.
- One session at a time: os_close_session the one you hold before opening
  another. After editing project Python open a fresh one — the running one
  imported the old code.
- Rollback is the default. Nothing persists until a commit; close, kill or
  death discard it. End experiments with os_rollback.
- Commit is a right the human grants. On `commit_not_allowed`: stop, say
  what you want to write and why, then poll os_session until allow_commit
  is true, not os_commit. `commit_forbidden` is never granted: read what
  you came for and roll back. Never env.cr.commit() or env.cr.rollback()
  in code: each has a tool.
- Work only in a session you opened or were handed. `not_owner` means ask
  for a handover, not a second session; never use a key you were not given.
- Never touch ~/.odoo-sheller/ and never call the daemon's admin endpoints.
- Read Odoo source with os_source, never with docker exec: os_help('code').
"""

HELP: dict[str, str] = {
    "sessions": """\
## Sessions

Open one with os_open_session, or adopt one the human hands you with
os_attach_session(session_id, write_key). You will normally be told both in a
message: the id alone is public and grants nothing, the write key is what allows
you to run code there. This server keeps your keys; you never pass them again.

One command runs at a time in a session. A second one is refused with
`session_busy` rather than queued — wait for the first to finish, or stop it with
os_interrupt.

os_exec waits about 40 seconds for an answer, because the host cuts a call off
not long after. A command still running then comes back as
`request_timed_out`, and it has not been stopped: it runs on, and the session
stays busy, until it ends or the daemon interrupts it at five minutes. Do not
run it again. os_history says when it finished and what it returned;
os_interrupt stops it now.

If a session is gone (`session_gone`), its process, namespace and variables died
with it. Nothing is restored by reopening. The refusal carries the target and a
journal link; open a replacement with os_open_session(replace=<old id>) and
decide for yourself which earlier commands are safe to run again — some of them
wrote to the database, and some were never meant to run twice. os_history is the
one tool a dead session still answers: it returns the transcript, and says so in
`session.state` and `session.gone`. Read the transcript, treat the session as
over.

A session may stay open for as long as you have more steps to run in it —
that is what a workspace is. But hold one at a time: when you move on to the
next piece of work, close the one you have with os_close_session before
opening another. An idle session still holds a process inside the container,
and a pile of them is the usual way this tool is left in a mess.
os_list_sessions shows what you are holding under `yours`; keep that list at
one. Closing discards uncommitted work, the same as rollback.

Editing the project's Python is the other reason to close. The interpreter
imported those files when the session started, and nothing makes it read them
again — not an upgrade, not a rollback, not a new command. A session that was
already open when you changed a `.py` is running the old code whatever the
file on disk now says, so every result it gives you is about code that no
longer exists. Close it and open a new one. Data, views and schema are the
opposite case: those an upgrade does pick up, without a new session —
os_help('modules').
""",
    "ownership": """\
## Ownership

Every session has one owner. Yours are owned by you; the human's are owned by
them, and you may read those but not type in them. A refusal reads `not_owner`:
stop, and ask the human to hand the session over rather than opening a second one
behind their back.

A handover moves the right to type without disturbing the session: the process,
the namespace and the open transaction all survive, so you continue exactly where
the human left off — their variables are still there, and so is their
uncommitted work (see the commit topic: `inherited_pending`). Ownership can move
back the same way, at any time, without warning to you.

The human is the admin. They can watch any session live, interrupt a command,
close or kill a session, hand ownership around and grant commit rights. You
cannot do any of that to a session you do not own, and you must not try.
""",
    "commit": """\
## Transactions and commit

Rollback is the default state of the world here. Everything you do lives in an
open transaction; closing the session, killing it or losing it discards the lot.
That is the safety of this tool, not an inconvenience: experiment freely and end
with os_rollback.

Writing to the database is a separate, granted right, and it gates os_commit
only — running code is never gated by it. os_exec always works once you own a
ready session; the only question is whether persisting it is allowed yet.

Your sessions open with the right off, and os_commit answers
`commit_not_allowed` until it is granted. The first time that happens:

1. Stop. Do not retry os_commit — spinning on it will not see the grant.
2. Tell the human, in plain words, what you want to write and why: which records,
   how many, and what would be wrong if it were rolled back instead.
3. They grant the right in the UI (session header keyboard, Grant commit).
   You will not be told in chat: call os_session and look at allow_commit.
   Repeat until it is true, then call os_commit.
4. Do not ask them whether they have granted it. os_session is how you know.

Once granted, the right stays granted — call os_commit directly for any later
commit in this same session, with no need to repeat this ritual first. It
lasts until the human revokes it, and does not survive a handover: check
again after one, the same way as the first time.

Uncommitted work travels with a session when ownership moves, and this is not
left to your memory: os_session reports `inherited_pending`, and the first
os_commit on such a session is refused as `inherited_pending` with the count.
Say whose work is in the transaction and what you are about to write, then
call os_commit(include_inherited=True) — or os_rollback to discard all of it.
Any commit or rollback clears the count; after that the transaction is yours
alone.

Both boundaries are tools, and only tools. Never write `env.cr.commit()` or
`env.cr.rollback()` in code you hand to os_exec. os_commit is not a wrapper
around `cr.commit()`: it is `flush_all()`, then `cr.commit()`, then
`invalidate_all(flush=False)`, and os_rollback is `invalidate_all(flush=False)`
then `cr.rollback()` — the order a discard needs. A bare `env.cr.commit()`
skips both halves: it ends the transaction and leaves `env` holding values
that are now stale, so what you read afterwards in that session can be wrong
in a way nothing announces. It is invisible, too. The journal records a
boundary when a tool draws one, so a commit buried in exec'd code is a write
to a real database that the human watching never sees happen.

What is fine inside code is `env.cr.savepoint()` — a nested block that rolls
itself back if the body raises and leaves the outer transaction untouched:

    with env.cr.savepoint():
        risky.write({"state": "done"})

That is a savepoint, not a commit: it persists nothing on its own, and
os_commit remains the only thing in this tool that writes.
""",
    "remote_server": """\
## Connecting to a remote Odoo server

A human may hand you a session that runs on a remote instance rather than a
local container — an odoo.sh build or a server reached by ssh, say. You cannot
open one yourself: no tool here takes a host, a build or a card, on purpose. You only ever receive one. That
includes reopening a dead one: os_open_session(replace=...) on a session that
ran remotely is refused and says so, because a journal records which instance it
was but not how to reach it. Ask for a handover rather than a way around.

Two things differ there, and os_session shows both as `kind` and `stage`:

- Commit is off for everyone until granted, not only for you. Locally a human
  owner may commit at will because they confirm in the UI; on someone's own
  Odoo that is not enough, so they grant it there the same way they grant it
  to you.
- On a `production` instance commit is refused outright, and the refusal is
  `commit_forbidden` rather than `commit_not_allowed`. The difference matters:
  `commit_not_allowed` means ask the human and then poll os_session, while
  `commit_forbidden` means nothing will ever grant it. Polling for that grant
  is an endless loop. Read what you came to read and end with os_rollback.
""",
    "limits": """\
## What you cannot do, and must not attempt

- Grant yourself commit rights, or call the daemon's admin endpoints.
- Take a session you were not handed, or use a key you were not given.
- Read or write ~/.odoo-sheller/ directly. Everything you need is in these tools.
- Count on Odoo 13 or 14. They open, with a warning, but are not fully tested:
  sessions, exec, commit, rollback and interrupt should work; os_run_test does
  not on 13 and says so.

These are not enforced by the keys you hold — they are the terms of using this
tool at all.
""",
    "orm": """\
## Writing idiomatic ORM code

A recordset is iterable, but a Python loop to pull one field, or to sum a
column, throws away idioms the ORM gives you for free — and reads worse to
whoever is watching the transcript.

- `records.mapped('name')` returns each record's `name` as a plain list.
  Prefer it over `[r.name for r in records]`.
- A dotted path follows a relation: `records.mapped('partner_id.bank_ids')`
  returns the union of every partner's banks, already de-duplicated, as one
  recordset — not a list of lists.
- Pass a callable for anything else: `records.mapped(lambda r: r.a + r.b)`,
  or `sum(records.mapped('qty'))` for a total.

`filtered()` narrows a recordset the same way — a function, a domain, or a
list of field names — and `sorted()` orders one, by a key function, a field
string, or the model's own default order with no argument at all. Neither
loads more from the database than `mapped()` does; none of the three is a
Python-side substitute for a real search domain.

Push filtering into `search()` itself rather than fetching broadly and
filtering in Python: `env['res.partner'].search([('is_company', '=', True)])`
reads and returns only what matches. `search_count(domain)` answers "how
many" without materializing any records at all — reach for it over
`len(records.search(domain))` whenever the records themselves are not needed.

A record that has an XML ID is fetched by that ID rather than searched for.
`env.ref` takes the module and the ID joined by a dot:

    env.ref("base.module_integration")          # the `integration` module
    env.ref(f"{module}.{xml_id}")               # when you are building one

It raises `ValueError` when nothing is there; pass `raise_if_not_found=False`
for an empty recordset instead. Prefer it over `search([('name', '=', ...)])`
whenever the XML ID is known — a name is data and can be changed by anyone,
while an XML ID is the identity the module itself declared.

Recordsets support the usual set operations directly — `|` union, `&`
intersection, `-` difference, `in` membership, `<=` / `<` / `>=` / `>` for
subset and superset — so two recordsets are combined or compared with an
operator, not a loop over ids with a manual dict to deduplicate.

Iterating a recordset yields one-record recordsets, not raw rows: `for r in
records: r.name` already works, no `r['name']` and no re-`browse()`ing an id
out of a dict. A helper that is only meant to run against a single record
should open with `self.ensure_one()` rather than assuming — it raises
immediately and clearly instead of the ambiguous behavior of reading a
multi-record field.
""",
    "code": """\
## Reading the code you are debugging

Reading source is one tool call, not a shell:

    os_source(path="integration_shopify")              # what files exist
    os_source(path="integration_shopify/models/external/external_payout.py",
              first=363, last=470)                     # 1-based, inclusive
    os_source(model="account.move", method="action_post")
    os_source(model="account.move", method="action_post", module="account")

A directory lists every file under it with its line count, so "what is in
this module" is answered before you guess a path. A file takes a
module-relative path or an absolute one inside an addons directory; anything
outside them is refused.

A method answers what a file cannot. `text` is the implementation that
actually runs, and `overrides` is the chain of modules that define it, in
resolution order — first is the one that runs, last is the base; everything
between them is reached by `super()`. On `account.move.action_post` that is
three modules, while the model's MRO is fifty-two classes, so the chain is
the answer and the MRO is noise. `module=` reads one link instead of the
winner, and naming a module that does not define the method comes back as
`not_in_that_module` with the chain to choose from. Without `method`, the
model form gives the class source and every module extending the model.

All of it needs a session you own, reads from the instance this session runs
in, and leaves nothing in its namespace.

Never `docker exec ... sed` for this: that reads a disk this session may not
even be running from, and a file cannot say which override is in effect.

The rest of this topic is for when a read is not enough — searching, walking
a class, comparing two overrides — and all of it goes in os_exec.

You are in a Python REPL inside the running instance, so the source is
readable — and reading it through the *loaded registry* answers a question
the filesystem cannot: which override actually wins.

    import inspect
    cls = type(env['res.partner'])
    [m.__module__ for m in cls.__mro__ if 'addons' in str(m.__module__)]

That lists every module that extends the model, in resolution order — so you
can see whose method is in front. Then read the one that matters:

    inspect.getsource(cls._compute_display_name)
    inspect.getsourcefile(cls._compute_display_name)

Prefer this over opening files by path. On disk every override sits side by
side and nothing says which is in effect; the MRO says.

A real method's source is longer than the result ceiling, so slice it rather
than fetching it whole and losing the end:

    src = inspect.getsource(cls._compute_display_name).splitlines()
    src[:40]

To read a file rather than a method — a view, a data file, a manifest, or a
.py you want by path and line range — use Odoo's own reader, which is
confined to the addons paths. It reads any file inside them, Python
included; only `filter_ext` narrows that:

    from odoo.tools import file_path, file_open
    file_path('sale')                      # the module's directory
    with file_open('sale/views/sale_views.xml') as f:
        head = f.read(4000)

A line range, the way you would reach for `sed`. Both an absolute path
inside an addons directory and a module-relative one work:

    with file_open('account_accountant/models/account_bank_statement.py') as f:
        lines = f.read().splitlines()
    "\n".join(lines[362:470])            # lines 363-470, 1-based

Never shell into the container for this — `docker exec ... sed` reads a
disk this session may not even be running from, and tells you nothing about
which override is in effect. `file_open` raises `FileNotFoundError` for a
path outside the addons directories (including one that climbs out with
`..`), refuses anything but `filter_ext` when you pass it, and will not
create files. Use it instead of bare `open()`.
""",
    "log": """\
## Seeing what Odoo logged

Every command reports `stderr_lines`: how many lines Odoo logged while it
ran. That is deliberately a number and not the lines — a log in every
response is context spent on output you did not ask for — but it is the one
thing you could not otherwise guess, so it is always there.

When the log is what you are after, ask for it: `os_exec(code, stderr=True)`
returns `stderr`, the lines in order, clipped from the end because the last
one usually says what happened. `stderr_truncated` means the daemon dropped
whole lines past its own ceiling; `truncated` means this server clipped
characters.

If `stderr_lines` was non-zero and you decide afterwards that you need the
lines, do **not** run the command again — it may have written to the
database, and a second run is a second write. Read them from the journal:
`os_journal(session_id)` has every line, interleaved with the commands by
time, including whatever a response clipped and including a traceback Odoo
logged rather than raised. `fmt="jsonl"` gives the raw records instead of
the transcript. A long journal comes back a page at a time — the end of it
by default, any other stretch with `first=` and `last=` (1-based line
numbers, as in os_source) — and says how many lines it has in all. Never
install a logging handler of your own to capture the log; it is collected
for you either way.

`os_run_test` on a class or a method returns its `stderr` without being
asked, because there the log *is* the answer — which test failed and why. A
whole module answers with `stderr_lines` instead and leaves its log in the
journal: os_help('tests').
""",
    "records": """\
## Reading a record

To see what a record actually holds, read it whole rather than naming the
fields you expect. `read()` with no arguments returns every field you may
read, computed ones included, so a field you did not think to ask for is
there:

    env['res.partner'].browse(11).read()[0]

The return is always a list, one dict per record, so a single record ends in
`[0]`. On a recordset the list is what you want — keep it and read the whole
set in one call rather than one record at a time.

Two things it does not do. Relations are not followed: a many2one comes back
as `(id, display_name)` and a one2many or many2many as a list of `ids`, so
going a level deeper is a second read on the related model. And a wide model
read whole is expensive and can be large — binary fields, long text, every
computed field evaluated — so read one record whole to learn its shape, then
`search_read` with the fields that turned out to matter for many.

`read()` on several records returns one dict each, in no guaranteed order,
and silently drops records that no longer exist. `read(load=None)` gives
bare ids for relations instead of the `(id, display_name)` pairs.
""",
    "modules": """\
## Installing and updating a module

A session loads the registry once, so a module's data, views and schema are
whatever they were when it opened. To pick up changes, upgrade the module in
the session; to add one that is not installed, install it the same way:

    Module = env['ir.module.module']
    Module.search([('name', '=', 'sale')]).button_immediate_upgrade()
    Module.search([('name', '=', 'sale_stock')]).button_immediate_install()

Install pulls in the module's dependencies, and an empty recordset means the
loader has never seen that module: call `Module.update_list()` first — it
scans the addons paths for manifests — then search again.

Either one commits on its own — it has to, to rebuild the registry — so it is
a write the commit gate does not cover. That is not a reason to ask
permission. You changed the code; the database has to catch up. An upgrade is
the consequence of your own edit, not a decision the human has to weigh, and
so is installing a module your work needs or running a migration script. Do
not ask, and do not wait for an answer: say what you are upgrading and run
it.

The one place this does not apply is a session running on someone else's
instance — os_session reports it as `kind` — where you were lent access, not
given the database. Never call either one there. Modules that depend on the
one you name are upgraded with it, and installing one installs whatever it
depends on.

This is how the module's migration scripts run, which is usually the point. A
script runs only if its version is above what `ir.module.module.latest_version`
records and no higher than the manifest's, so a database already at the
manifest version redoes schema and data but runs no script.

Edited Python is not picked up: the process imported those files at startup
and an upgrade does not re-import them. For a change in a `.py`, close this
session and open a new one — os_help('sessions').
""",
    "jobs": """\
## Delayed jobs

`with_delay()` enqueues a job; this session will not run the queue for you.
To run a delayed method inline instead of enqueueing it, put
`queue_job__no_delay` on the environment context. Nested `with_delay()`
calls inherit it, so the whole chain runs on the spot:

    env = env(context=dict(env.context, queue_job__no_delay=True))
    record.with_delay().do_work()

A recordset you already hold still has the old context: call
`.with_context(queue_job__no_delay=True)` on it before `with_delay()`.
""",
    "tests": """\
## Running a test

os_run_test takes three forms:

- `module` — every standard test in that module, at_install and
  post_install, the set `--test-tags /module` runs. Tests tagged `-standard`
  or `external` are left out: name their class to run one of those.
- `module.TestClass` — one class.
- `module.TestClass.test_method` — one method.

Every answer carries `failed`: one entry per failing test, `{"test": spec,
"kind": "failure" | "error"}`, where `test` is ready to pass straight back to
os_run_test. A failed subtest is listed as its method, a class whose
setUpClass failed as the class, and a test file whose setUpModule failed as
the whole module — no tag can name a file. To find out why one failed, run
that spec on its own: a class or a method answers with its log tail in
`stderr`.

A whole module answers with counts and `failed` only. Its log is thousands
of lines, so it comes back as `stderr_lines`, a count, and stays in the
journal — os_journal(session_id) has every line if you really need it. Do
not rerun a module to see its log; rerun the failed specs.

Unlike os_exec, it always
opens its own brand-new session first — it never reuses a session you already
have — so there is nothing of yours it can discard. That session closes itself
the moment the run settles: do not call os_close_session on it, and do not
count it against yourself in os_list_sessions. To run everything a module
has, pass the module rather than calling os_run_test once per class.

When the class name is unknown, call os_list_tests(module) rather than inventing
names. Do not open a session per method unless a single method is the point.
Do not fire a list of classes as parallel os_run_test calls: one module run
covers them in one session.

Odoo's own test runner rolls back whatever transaction a session's cursor is
holding before it runs tests. This only matters if you call os_run_test's
underlying session a second time after using os_exec in it — the response's
`discarded_pending` field says whether that happened, so watch it rather than
assume nothing was lost.

Without a `timeout`, the ceiling follows the spec: 30 seconds for a method,
300 for a class, 1800 for a module. Pass one only to go past that — the most
is 3600 — and pad your estimate: Odoo's own test framework can add up to 10
extra seconds per test class if one leaves a subprocess running. It is the
ceiling for the run, not for the call: a run that is still going when the
call has to answer comes back as `status: "running"`, below.

A run longer than about a minute cannot be answered in one call: the host
cuts a tool call off well before that, whatever timeout you passed. So
os_run_test hands back `{"status": "running", "session_id": ...}` instead —
that is not a failure, and the run is still going in the container.

When you see it, call os_test_result(session_id). That waits too, and answers
either with the finished outcome or with `status: "running"` again — in which
case call it again straight away. There is nothing to pause between calls,
and nothing to clean up afterwards: the session closes itself.

Never answer a `status: "running"` by calling os_run_test again. That starts
a second, duplicate run on top of the first.

To run a test in a session a human handed you rather than a fresh one, pass
os_run_test(session_id=...). On a remote instance that is the only way, since
no tool here can name one. That session is not yours: it does not close
itself, and you do not close it either. Watch `discarded_pending` there — if
the human left work in it, Odoo's own runner rolled that back before testing.
Passing both a session_id and a container is refused rather than guessed at.
""",
    "watching": """\
## Being watched

Everything is journalled with its author: every command, its output, every
transaction boundary and every handover. The human watches sessions live and
reads the transcript afterwards. Write commands that are legible on their own,
prefer small steps over one large opaque script, and say what you are doing when
it is not obvious from the code.
""",
}

# A topic that was renamed still answers to what it was called: an agent that
# read the old name once, or a transcript that recorded it, must not land on
# no_such_topic. Kept out of HELP itself so os_help() lists each topic once.
HELP_ALIASES = {"remote": "remote_server"}


mcp = MCPServer("odoo-sheller", instructions=INSTRUCTIONS)

# session id -> write key, for the sessions this server may type into.
_keys: dict[str, str] = {}


def _clip(text: str | None, limit: int) -> tuple[str | None, bool]:
    """Agent context is the scarce resource here; the journal keeps the rest."""
    if text is None or len(text) <= limit:

        return text, False

    return text[:limit], True


def _clip_tail(text: str | None, limit: int) -> tuple[str | None, bool]:
    """Keep the end, not the beginning.

    For a test run's log the last line is the one worth having — the summary,
    or the failure that ended it. Clipping from the front hands back the test
    framework's boot chatter and drops the answer.
    """
    if text is None or len(text) <= limit:

        return text, False

    return text[-limit:], True


def _default_session(session_id: str | None) -> str | Any:
    if session_id:

        return session_id
    if len(_keys) == 1:

        return next(iter(_keys))
    if not _keys:

        return {
            "error": "no_session",
            "recovery": "call os_open_session first, or os_attach_session with an id and key",
        }

    return {
        "error": "ambiguous_session",
        "sessions": sorted(_keys),
        "recovery": "pass session_id explicitly: this server owns more than one session",
    }


async def _call(
    method: str,
    path: str,
    session_id: str | None = None,
    client_timeout: float | None = None,
    **kwargs,
) -> Any:
    headers = {}
    if session_id and session_id in _keys:
        headers["X-OS-Session-Key"] = _keys[session_id]
    try:
        async with httpx.AsyncClient(
            base_url=DAEMON_URL, timeout=client_timeout or CALL_WAIT
        ) as client:
            response = await client.request(method, path, headers=headers, **kwargs)
    except httpx.TimeoutException:
        # The daemon is still working — it never stopped answering. Telling
        # the agent to go start it would be wrong, and a retry would kick off
        # a brand-new run instead of checking on the slow one already going.
        logger.warning("request timed out waiting for the daemon")

        return {
            "error": "request_timed_out",
            "recovery": (
                "the daemon is still working, not down — call os_history or "
                "os_journal on the session to see whether it finished, rather "
                "than retrying this call"
            ),
        }
    except Exception as exc:  # noqa: BLE001 - any transport failure means the same thing
        logger.warning("daemon unreachable: %s", exc)

        return {
            "error": "daemon_unreachable",
            "url": DAEMON_URL,
            "recovery": "ask the human to start it: uv run python -m odoo_sheller",
        }

    if response.status_code < 400:
        if response.headers.get("content-type", "").startswith("application/json"):

            return response.json()

        return {"text": response.text}

    detail = response.json().get("detail") if response.headers.get(
        "content-type", ""
    ).startswith("application/json") else response.text
    if isinstance(detail, dict):

        return detail

    return {"error": f"http_{response.status_code}", "message": detail}


@mcp.tool(
    description="List running Docker containers with what the probe found in each.",
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_list_containers() -> Any:
    containers = await _call("GET", "/api/containers")
    if isinstance(containers, dict):

        return containers
    probed = []
    for container in containers:
        probe = await _call("POST", "/api/probe", json={"container": container["name"]})
        probed.append({**container, "probe": probe})

    return {"containers": probed, "count": len(probed)}


@mcp.tool(
    description="List every live session with its owner and state. Read-only.",
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_list_sessions() -> Any:
    sessions = await _call("GET", "/api/sessions")
    if isinstance(sessions, dict):

        return sessions

    # Test sessions close themselves, so keys outlive the sessions they were
    # for. Drop the ones the daemon no longer has rather than reporting them.
    live = {session["id"] for session in sessions}
    for lost in [held for held in _keys if held not in live]:
        del _keys[lost]

    listed = [{**session, "yours": session["id"] in _keys} for session in sessions]

    return {
        "sessions": listed,
        "count": len(listed),
        "yours": sorted(_keys),
    }


@mcp.tool(
    description=(
        "Open a session you own. Pass replace=<lost session id> to reopen on the same "
        "target as a session that is gone; its variables are not restored."
    ),
)
async def os_open_session(
    container: str | None = None,
    database: str | None = None,
    odoo_bin: str | None = None,
    replace: str | None = None,
) -> Any:
    opened = await _call(
        "POST",
        "/api/sessions",
        json={
            "container": container,
            "database": database,
            "odoo_bin": odoo_bin,
            "replace": replace,
            "owner": {"kind": "agent", "label": AGENT_LABEL},
            "allow_commit": False,
        },
    )
    if opened.get("error"):

        return opened
    _keys[opened["id"]] = opened.pop("write_key")

    return opened


@mcp.tool(
    description=(
        "Current state of a session: owner, whether it is ready, and whether "
        "commit has been granted. Read-only. After asking for Grant commit, "
        "call this until allow_commit is true — do not wait for a chat message."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_session(session_id: str | None = None) -> Any:
    target = _default_session(session_id)
    if isinstance(target, dict):

        return target

    return await _call("GET", f"/api/sessions/{target}", target)


@mcp.tool(
    description=(
        "Adopt a session a human handed over, using the id and write key they gave "
        "you. The id alone grants nothing; never attach with a key you were not given."
    ),
)
async def os_attach_session(session_id: str, write_key: str) -> Any:
    _keys[session_id] = write_key
    described = await _call("GET", f"/api/sessions/{session_id}", session_id=session_id)
    if described.get("error"):
        _keys.pop(session_id, None)

        return described
    # The daemon says what the key is worth here. A wrong one used to attach
    # without complaint and fail on the first command instead.
    status = described.pop("key_status", "owner")
    if status != "owner":
        _keys.pop(session_id, None)

        return {
            "error": "not_owner",
            "session_id": session_id,
            "key_status": status,
            "owner": described.get("owner"),
            "recovery": (
                "that key no longer types here — ownership moved since it was "
                "issued; ask the human for the current one"
                if status == "former_owner" else
                "that key does not open this session; ask the human to hand it "
                "over again — a key is shown once, at the handover"
            ),
        }

    return described


@mcp.tool(
    description=(
        "Run Python in a session you own. Blocks until the command finishes. "
        "Variables persist between calls; one command runs at a time. Returns "
        "stdout, the returned value, any error, and `stderr_lines` — how many "
        "lines Odoo logged while the command ran. Pass stderr=True to get "
        "those lines back as `stderr` (clipped from the end); they cost "
        "context, so ask when the log is what you are after. Either way the "
        "log is kept: read it with os_journal instead of running the command "
        "again. Never install a logging handler to capture it."
    ),
)
async def os_exec(
    code: str, session_id: str | None = None, stderr: bool = False
) -> Any:
    target = _default_session(session_id)
    if isinstance(target, dict):

        return target
    result = await _call("POST", f"/api/sessions/{target}/exec", target, json={"code": code})
    if result.get("error") == "request_timed_out":
        # Our wait ran out, not the command: it runs on in the container.

        return {
            **result,
            "session_id": target,
            "recovery": (
                "the command is still running and the session stays busy until "
                "it ends, or until the daemon interrupts it at five minutes; do "
                "not run it again — os_history shows when it finished and what "
                "it returned, os_interrupt stops it now"
            ),
        }
    if result.get("error") and "stdout" not in result:

        return result

    stdout, stdout_clipped = _clip(result.get("stdout"), MAX_STDOUT)
    value, value_clipped = _clip(result.get("result"), MAX_RESULT)
    log = result.get("stderr") or []
    clipped = stdout_clipped or value_clipped
    answer = {
        "stdout": stdout,
        "result": value,
        "error": result.get("error"),
        # One number rather than the lines: it says a log exists — which is
        # the thing an agent cannot guess — without billing the context for
        # content nobody asked for.
        "stderr_lines": len(log),
        "duration": result.get("duration"),
    }
    if stderr:
        # Clipped from the end, as in os_run_test: on a long command the last
        # line is the one that says what happened.
        text, stderr_clipped = _clip_tail("\n".join(log), MAX_STDOUT)
        clipped = clipped or stderr_clipped
        answer["stderr"] = text
        answer["stderr_truncated"] = bool(result.get("stderr_truncated"))
    answer["truncated"] = clipped
    # The journal is the way back to a log that was not asked for, and to
    # anything the clipping dropped — without running the command again.
    answer["journal"] = (
        f"/api/journals/{target}" if clipped or (log and not stderr) else None
    )

    return answer


@mcp.tool(
    description=(
        "Read Odoo source from inside the running instance, instead of "
        "shelling into the container — that reads a disk this session may not "
        "run from, and cannot say which override is in effect. Three forms. "
        "A directory: os_source(path='sale') lists every file in the module "
        "with its line count. A file: os_source(path='sale/models/sale_order"
        ".py', first=100, last=180) — module-relative or absolute inside an "
        "addons directory, 1-based inclusive, anything outside refused. A "
        "method: os_source(model='sale.order', method='action_confirm') "
        "returns the source that actually runs plus `overrides`, the modules "
        "defining it in resolution order — first is the one that runs, last "
        "is the base. Add module='sale' to read that link instead. Needs a "
        "session you own; leaves no names in its namespace."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_source(
    path: str | None = None,
    model: str | None = None,
    method: str | None = None,
    module: str | None = None,
    first: int | None = None,
    last: int | None = None,
    session_id: str | None = None,
) -> Any:
    if path and model:

        return {
            "error": "ambiguous_request",
            "recovery": "pass path (a file) or model (the code that runs), not both",
        }
    if not path and not model:

        return {
            "error": "nothing_to_read",
            "recovery": (
                "pass path='module/dir/file.py' with an optional first/last line "
                "range, or model='res.partner' with an optional method"
            ),
        }
    if method and not model:

        return {"error": "method_without_model", "recovery": "pass model= as well"}
    if module and not method:

        return {
            "error": "module_without_method",
            "recovery": (
                "module= picks one link out of a method's override chain, so "
                "name the method too"
            ),
        }
    target = _default_session(session_id)
    if isinstance(target, dict):

        return target

    code = _source_snippet(path, model, method, first, last, module)
    # A read: journalled like any command, but not counted as work in the
    # transaction — or a handover would warn about it, and a test run report
    # it as discarded.
    result = await _call(
        "POST", f"/api/sessions/{target}/exec", target,
        json={"code": code, "read_only": True},
    )
    if result.get("error") and "stdout" not in result:

        return result
    failure = result.get("error")
    if failure:

        return {
            "error": "unreadable",
            "reason": f"{failure.get('type')}: {failure.get('message')}",
            "recovery": (
                "a path must be inside an addons directory and a model must be "
                "in the registry; os_help('code') has the rules"
            ),
        }
    try:
        # The session hands back the repr of the JSON string the snippet
        # returned, which is exactly one literal.
        payload = json.loads(ast.literal_eval(result.get("result") or "'{}'"))
    except (ValueError, SyntaxError):

        return {"error": "unreadable", "reason": "the read produced no payload"}
    if payload.get("error"):

        return payload
    if payload.get("directory"):

        return _clip_listing(payload)
    text, clipped = _clip(payload.get("text") or "", MAX_SOURCE)
    payload["text"] = text
    payload["truncated"] = clipped
    if clipped:
        payload["recovery"] = (
            "narrow it with first= and last=, or read one method by model="
        )

    return payload


def _clip_listing(payload: dict) -> dict:
    """A module can hold hundreds of files; a listing is still an answer."""
    entries = payload.get("entries") or []
    if len(entries) > MAX_LISTING:
        payload["entries"] = entries[:MAX_LISTING]
        payload["truncated"] = True
        payload["recovery"] = (
            f"{len(entries)} files: ask for a subdirectory, e.g. "
            f"path='{payload.get('path')}/models'"
        )
    else:
        payload["truncated"] = False

    return payload


def _source_snippet(path, model, method, first, last, module=None) -> str:
    """Python for the session to run, returning the payload as a JSON string.

    Everything happens inside one function so its imports and locals never
    reach the session, and the function pops its own name on the way out: a
    session is the human's workspace and a read has no business appearing in
    it. The value is returned rather than printed — this server's stdout is
    JSON-RPC, and a habit of printing is how that gets broken.
    """
    if path:
        # Python literals, not JSON ones: `json.dumps(None)` is `null`, which
        # is a NameError in the container and nowhere else — a mocked session
        # never runs this, so only a live one ever said so.
        opening = repr(int(first)) if first else "1"
        closing = repr(int(last)) if last else "len(lines)"
        body = f"""\
    import os
    from odoo.tools import file_open, file_path
    target = file_path({path!r})
    if os.path.isdir(target):
        # Entries are rooted at the module, not at the target's parent, so
        # every path that comes back can be handed straight back in.
        root = target
        while root != os.path.dirname(root):
            if os.path.exists(os.path.join(root, "__manifest__.py")):
                break
            root = os.path.dirname(root)
        base = os.path.dirname(root)
        # "What is in this module" is the question that comes before "read me
        # line 363", and it must not be the one thing left to the shell.
        entries = []
        for root, dirs, names in os.walk(target):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(names):
                if name.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, base)
                try:
                    with open(full, "rb") as handle:
                        count = sum(1 for _ in handle)
                except OSError:
                    count = None
                entries.append({{"path": rel, "lines": count}})
        payload = {{"path": {path!r}, "directory": True,
                   "entries": entries, "files": len(entries)}}
    else:
        with file_open({path!r}) as handle:
            lines = handle.read().splitlines()
        first = {opening}
        last = min({closing}, len(lines))
        payload = {{"path": {path!r}, "total_lines": len(lines),
                   "first": first, "last": last,
                   "text": chr(10).join(lines[first - 1:last])}}
"""
    else:
        # Every class in the MRO extends the model; almost none of them define
        # the method. On account.move.action_post that is 3 classes out of 52,
        # and the other 49 are noise in the answer.
        body = f"""\
    import inspect
    cls = type(env[{model!r}])
    method = {method!r}
    wanted = {module!r}

    def addon(klass):
        parts = klass.__module__.split(".")
        return parts[2] if klass.__module__.startswith("odoo.addons.") else parts[0]

    def link(klass):
        fn = klass.__dict__[method]
        try:
            line = inspect.getsourcelines(fn)[1]
        except (OSError, TypeError):
            line = None
        return {{"module": addon(klass), "where": klass.__module__,
                "file": inspect.getsourcefile(klass), "line": line}}

    if method:
        definers = [klass for klass in cls.__mro__ if method in klass.__dict__]
        chain = [link(klass) for klass in definers]
        if wanted:
            picked = [klass for klass in definers if addon(klass) == wanted]
            if not picked:
                payload = {{"error": "not_in_that_module", "model": {model!r},
                           "method": method, "module": wanted, "overrides": chain,
                           "recovery": "pick one of the modules in overrides, "
                                       "or drop module= for the one that runs"}}
                return json.dumps(payload)
            obj = picked[0].__dict__[method]
            owner = picked[0]
        else:
            obj = getattr(cls, method)
            owner = definers[0] if definers else cls
        payload = {{"model": {model!r}, "method": method, "module": wanted,
                   "file": inspect.getsourcefile(obj),
                   "line": inspect.getsourcelines(obj)[1],
                   "defined_in": getattr(owner, "__module__", None),
                   "overrides": chain,
                   "text": inspect.getsource(obj)}}
    else:
        payload = {{"model": {model!r}, "method": None,
                   "file": inspect.getsourcefile(cls),
                   "line": inspect.getsourcelines(cls)[1],
                   "defined_in": cls.__module__,
                   "overrides": [addon(klass) for klass in cls.__mro__
                                 if klass.__module__.startswith("odoo.addons.")],
                   "text": inspect.getsource(cls)}}
"""

    return (
        "def _os_read(_os_ns=globals()):\n"
        # First, not last: the call has already resolved the object, and a
        # read that raises must not leave the name behind either.
        '    _os_ns.pop("_os_read", None)\n'
        "    import json\n"
        + body
        + "    return json.dumps(payload)\n"
        "\n"
        "_os_read()"
    )


@mcp.tool(
    description=(
        "The parts of this server's instructions a host did not deliver. "
        "Hosts cut server instructions at 2048 characters, which is a small "
        "fraction of what there is to say, so the rest is here by topic: "
        "sessions, ownership, commit, remote_server, limits, orm, code, "
        "log, records, modules, jobs, tests, watching. Call with no topic for the "
        "list. Read-only, no session, no side effects — cheaper than working "
        "around a rule you were never shown."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_help(topic: str | None = None) -> Any:
    if topic is None:

        return {
            "topics": list(HELP),
            "instructions_truncated_at": INSTRUCTION_CAP,
            "recovery": "call os_help(topic) for one of these",
        }
    topic = HELP_ALIASES.get(topic, topic)
    text = HELP.get(topic)
    if text is None:

        return {
            "error": "no_such_topic",
            "topic": topic,
            "topics": list(HELP),
            "recovery": "call os_help(topic) with one of these, or os_help() for the list",
        }

    return {"topic": topic, "text": text}


@mcp.tool(
    description=(
        "List test classes and methods in one addon, already shaped as "
        "os_run_test specs (module.TestClass / module.TestClass.test_method). "
        "One module per call. This is files on disk, not 'installed in this "
        "database'. Read-only; does not open a session."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_list_tests(module: str, container: str | None = None) -> Any:
    target = container
    if not target:
        session_id = _default_session(None)
        if isinstance(session_id, dict):
            refusal = dict(session_id)
            if refusal.get("error") == "no_session":
                refusal["recovery"] = "pass container, or open a session first"
            elif refusal.get("error") == "ambiguous_session":
                refusal["recovery"] = (
                    "pass container explicitly: this server owns more than one session"
                )

            return refusal
        described = await _call("GET", f"/api/sessions/{session_id}", session_id)
        if described.get("error"):

            return described
        if described.get("kind", "docker") != "docker":
            # The catalogue is read from a local container's disk; a build id,
            # or a card's name, in that slot would go to `docker exec` and fail
            # obscurely.

            return {
                "error": "not_a_container",
                "session_id": session_id,
                "recovery": (
                    "os_list_tests reads a local container's files; on a remote "
                    f"instance, list the module's tests with os_source(path='{module}/tests') "
                    "and read the file that holds the class"
                ),
            }
        target = described["container"]

    return await _call(
        "GET",
        f"/api/containers/{target}/tests",
        params={"module": module},
    )


RUN_TEST_DESCRIPTION = (
    "Run Odoo tests by name: 'module' for every standard test in that module, "
    "'module.TestClass' for one class, 'module.TestClass.test_method' for one "
    "method. A whole module answers with counts and `failed` — each failure as "
    "a spec to pass back here — and leaves its log in the journal; a class or "
    "a method also returns its log. Always opens its "
    "own brand-new session (owner agent, allow_commit false) rather than "
    "reusing one you already have, so there is never anything pending to "
    "lose, and that session closes itself once the run settles — there is "
    "nothing to clean up. Pass session_id instead to run in a session a "
    "human handed you — the only way onto a remote instance — and then it "
    "is theirs, closed neither by you nor by itself. "
    "A run too long to answer in one call "
    "comes back as {\"status\": \"running\", \"session_id\": ...}, which is "
    "not a failure: call os_test_result(session_id) to wait for it, and "
    "never call this tool again for the same run. Without a timeout the "
    "ceiling follows the spec — 30s for a method, 300s for a class, 1800s "
    "for a module, at most 3600 — and it is the ceiling for the run itself, "
    "not for this call."
)


@mcp.tool(description=RUN_TEST_DESCRIPTION)
async def os_run_test(
    test: str,
    container: str | None = None,
    database: str | None = None,
    odoo_bin: str | None = None,
    timeout: float | None = None,
    session_id: str | None = None,
) -> Any:
    if timeout is None:
        timeout = DEFAULT_TEST_TIMEOUTS[_spec_form(test)]
    if session_id and (container or database or odoo_bin):
        # One says where to run, the other says where to open, and the two can
        # name different places. Refuse rather than silently pick.
        return {
            "error": "ambiguous_target",
            "recovery": (
                "pass session_id to run in a session you were handed, or a "
                "container to open a fresh one — not both"
            ),
        }
    if not 0 < timeout <= MAX_TEST_TIMEOUT:
        # Checked before anything is opened: a doomed ceiling would otherwise
        # cost a whole session start to earn a raw validation error.
        return {
            "error": "invalid_timeout",
            "timeout": timeout,
            "recovery": (
                f"pass a timeout between 0 and {MAX_TEST_TIMEOUT:.0f} seconds; "
                "a whole class usually wants a few hundred"
            ),
        }

    # The host times the whole tool call, so the budget has to cover opening
    # the session as well as waiting for the run. Spending it on the run alone,
    # after a registry load that already took seconds, overshoots and the call
    # is killed before it can hand back the session id.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MCP_CALL_BUDGET

    if session_id:
        # A session a human handed over — the only way onto a remote instance,
        # since no tool here can name one. Not ours to close, and not opened
        # with autoclose, so it stays exactly as it was lent.
        return await _run_test_in(session_id, test, timeout, deadline, ours=False)

    # Container and database do not identify a session — several may be
    # opening on the same target at once — so the token is what finds this one
    # again if the open call is the thing that times out.
    client_token = f"{AGENT_LABEL}-{secrets.token_urlsafe(8)}"
    opened = await _call(
        "POST",
        "/api/sessions",
        # Bounded by the same budget as everything else: waiting out the
        # daemon's full ceiling only means the host kills the call first, and
        # then even the client_token below never reaches the caller.
        client_timeout=min(SESSION_START_CEILING + 10, MCP_CALL_BUDGET),
        json={
            "container": container,
            "database": database,
            "odoo_bin": odoo_bin,
            "owner": {"kind": "agent", "label": AGENT_LABEL},
            "allow_commit": False,
            "client_token": client_token,
            # It exists to run one test. Nobody has to remember to close it.
            "autoclose": True,
        },
    )
    if opened.get("error"):
        if opened.get("error") == "request_timed_out":

            return {
                **opened,
                "client_token": client_token,
                "recovery": (
                    "the session may still have opened — find it with "
                    "os_list_sessions by this client_token before opening "
                    "another, and ask the human to close it: this server never "
                    "received its write key"
                ),
            }

        return opened
    session_id = opened["id"]
    _keys[session_id] = opened.pop("write_key")

    return await _run_test_in(session_id, test, timeout, deadline, ours=True)


def _spec_form(test: str) -> str:
    """What a test spec names: a module, a class in it, or one method."""

    return ("module", "class", "method")[min(test.count("."), 2)]


async def _run_test_in(
    session_id: str, test: str, timeout: float, deadline: float, ours: bool
) -> Any:
    """Run one test in a session and shape the outcome.

    `ours` says whether this server opened the session. It changes nothing
    about the run, and everything about what the answer may promise: a session
    we opened closes itself, and one we were lent is the human's to end.
    """
    loop = asyncio.get_running_loop()
    # Whatever is left of the budget, and never more: a margin added on top of
    # a cap defeats the cap. The daemon still gets the full ceiling the caller
    # asked for — only our own waiting is capped, so the run is never cut short.
    # Two seconds over the daemon's own ceiling keeps a short run from racing
    # its 504 against our timeout.
    run_leg = max(min(timeout + 2, deadline - loop.time()), RUN_START_GRACE)
    result = await _call(
        "POST", f"/api/sessions/{session_id}/run_test", session_id,
        client_timeout=run_leg,
        json={"test": test, "timeout": timeout},
    )
    if result.get("error") == "request_timed_out":
        afterwards = (
            "that session closes itself when the run ends, so there is "
            "nothing to clean up"
            if ours else
            "that session is the human's: it stays open after the run, and "
            "you do not close it"
        )

        return {
            "status": "running",
            "session_id": session_id,
            "test": test,
            "recovery": (
                "the run is still going in the container — call "
                f'os_test_result("{session_id}") to wait for it; {afterwards}'
            ),
        }
    if result.get("error") and "stdout" not in result:

        return {"session_id": session_id, **result}

    test_info = result.get("test") or {}
    stdout, stdout_clipped = _clip(result.get("stdout"), MAX_STDOUT)
    log, stderr_clipped = _test_log(test, result)
    truncated = stdout_clipped or stderr_clipped

    return {
        "session_id": session_id,
        "tests_run": test_info.get("tests_run"),
        "failures": test_info.get("failures"),
        "errors": test_info.get("errors"),
        "skipped": test_info.get("skipped"),
        "success": test_info.get("success"),
        "failed": result.get("failed", []),
        "stdout": stdout,
        **log,
        "error": result.get("error"),
        "duration": result.get("duration"),
        "discarded_pending": result.get("discarded_pending"),
        # Two different losses: the daemon dropping whole lines past its own
        # ceiling, and this server clipping characters to spare your context.
        "stderr_truncated": bool(result.get("stderr_truncated")),
        "truncated": truncated,
        "journal": (
            f"/api/journals/{session_id}"
            if truncated or "stderr_lines" in log else None
        ),
    }


def _test_log(test: str, result: dict) -> tuple[dict, bool]:
    """The log of a run as an agent should get it, and whether it was clipped.

    For a class or a method the log is the answer — which test failed and
    why — so its tail comes back. A whole module logs thousands of lines that
    `failed` already distils, so it comes back as a count and stays in the
    journal; os_journal has every line.
    """
    lines = result.get("stderr") or []
    if "." not in test:

        return {"stderr_lines": len(lines)}, False
    # The tail: on a long run the last line is the summary, and clipping from
    # the front would drop it.
    stderr, clipped = _clip_tail("\n".join(lines), MAX_STDOUT)

    return {"stderr": stderr}, clipped


@mcp.tool(
    description=(
        "Wait for a test run started by os_run_test and return its outcome. "
        "Blocks while the run is still going, then answers with tests_run, "
        "failures, errors, skipped and success — or says the run is still "
        "going, in which case call it again straight away. Works after the "
        "session has closed itself, and needs no write key. Read-only."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_test_result(session_id: str) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MCP_CALL_BUDGET
    while True:
        feed = await _call("GET", f"/api/sessions/{session_id}/history")
        if feed.get("error"):

            return feed

        runs = [
            entry for entry in (feed.get("entries") or [])
            if entry.get("kind") == "run_test"
        ]
        if runs and (runs[-1].get("result") is not None):

            return {"status": "done", **_history_run_test_entry(runs[-1])}

        gone = (feed.get("session") or {}).get("state") == "gone"
        if gone:
            # The session ended without the run ever settling: the process
            # died mid-run. Saying "running" here would be a poll forever.

            return {
                "status": "lost",
                "session_id": session_id,
                "journal": f"/api/journals/{session_id}",
                "recovery": (
                    "the run died with its container process — read the "
                    "journal for the log tail, then start it again"
                ),
            }
        if loop.time() >= deadline:

            return {
                "status": "running",
                "session_id": session_id,
                "recovery": (
                    "still going — call os_test_result again straight away; "
                    "each call waits, so there is no need to pause between them"
                ),
            }
        await asyncio.sleep(TEST_RESULT_POLL)


def _boundary_result(answer: Any) -> Any:
    """Wire frames are for the daemon; an agent needs the outcome."""
    if answer.get("error") and "stdout" not in answer:

        return answer  # a refusal, already shaped

    return {"ok": not answer.get("error"), "error": answer.get("error")}


@mcp.tool(
    description=(
        "Discard the open transaction. Nothing is written to the database, and the "
        "namespace survives. This is the normal end of an experiment."
    ),
)
async def os_rollback(session_id: str | None = None) -> Any:
    target = _default_session(session_id)

    if isinstance(target, dict):

        return target

    return _boundary_result(await _call("POST", f"/api/sessions/{target}/rollback", target))


@mcp.tool(
    description=(
        "Write the open transaction to the database — the only tool here that "
        "persists anything. Refused with commit_not_allowed unless a human has "
        "granted this session the right: if refused, say what you want to write "
        "and why, then poll os_session until allow_commit is true. Retrying "
        "os_commit will not see the grant. On a session handed to you with work "
        "already pending, refused once as inherited_pending: that work is the "
        "previous owner's and a commit writes it too, so say so, then call with "
        "include_inherited=True."
    ),
    annotations=ToolAnnotations(destructive_hint=True),
)
async def os_commit(
    session_id: str | None = None, include_inherited: bool = False
) -> Any:
    target = _default_session(session_id)

    if isinstance(target, dict):

        return target
    if not include_inherited:
        # A handover carries the open transaction, so the first commit in a
        # lent session would write the human's work under the agent's name.
        # The rule was documented and an agent still had to remember it; this
        # is the same rule, spent as one refusal instead.
        described = await _call("GET", f"/api/sessions/{target}", target)
        inherited = (described or {}).get("inherited_pending") or 0
        if inherited:

            return {
                "error": "inherited_pending",
                "session_id": target,
                "inherited_pending": inherited,
                "pending_commands": described.get("pending_commands"),
                "reason": (
                    f"{inherited} command(s) in this transaction were run by the "
                    "previous owner; committing writes their work too"
                ),
                "recovery": (
                    "tell the human whose work is in the transaction and what "
                    "you are about to write, then call "
                    "os_commit(include_inherited=True) — or os_rollback to "
                    "discard all of it"
                ),
            }

    return _boundary_result(await _call("POST", f"/api/sessions/{target}/commit", target))


@mcp.tool(
    description=(
        "Interrupt the command running in a session you own. The session, its "
        "namespace and its transaction all survive."
    ),
)
async def os_interrupt(session_id: str | None = None) -> Any:
    target = _default_session(session_id)

    return target if isinstance(target, dict) else await _call(
        "POST", f"/api/sessions/{target}/interrupt", target
    )


@mcp.tool(
    description="Close a session you own. Uncommitted work is discarded.",
    annotations=ToolAnnotations(destructive_hint=True),
)
async def os_close_session(session_id: str | None = None) -> Any:
    target = _default_session(session_id)
    if isinstance(target, dict):

        return target
    closed = await _call("DELETE", f"/api/sessions/{target}", target)
    _keys.pop(target, None)

    return closed


def _actor(actor: dict | None) -> str | None:
    """`kind:label`, with the label falling back to the kind.

    It used to index `label`. A session handed over as `{"kind": "agent"}` —
    which the daemon accepted until it began naming them — made this raise
    KeyError, so os_history failed on the very session an agent was given.
    """
    if not actor:

        return None
    kind = actor.get("kind") or "unknown"

    return f"{kind}:{actor.get('label') or kind}"


def _history_run_test_entry(entry: dict) -> dict:
    """A run_test entry, in the shape os_run_test answers with.

    Journaled precisely so a transport timeout on a long class doesn't lose
    the outcome — the agent recovers it from here instead of nowhere.
    """
    result = entry.get("result") or {}
    test = result.get("test") or {}
    spec = ".".join(
        part for part in (entry.get("module"), entry.get("test_class"), entry.get("test_method"))
        if part
    )
    stdout, out_clipped = _clip(result.get("stdout"), MAX_STDOUT)
    # The same rule os_run_test answers by, so a run recovered later reads
    # the same as one answered at once.
    log, stderr_clipped = _test_log(spec, result)
    shaped = {
        "n": entry.get("ordinal"),
        "test": spec,
        "status": entry.get("status"),
        "tests_run": test.get("tests_run"),
        "failures": test.get("failures"),
        "errors": test.get("errors"),
        "skipped": test.get("skipped"),
        "success": test.get("success"),
        "failed": result.get("failed"),
        "stdout": stdout or None,
        "stderr": log.get("stderr") or None,
        "stderr_lines": log.get("stderr_lines"),
        # Whole lines the daemon dropped, as opposed to the characters clipped
        # just above. Absent from journals written before it existed.
        "stderr_truncated": result.get("stderr_truncated"),
        "error": result.get("error"),
        "duration": round(result["duration"], 3) if result.get("duration") is not None else None,
        "actor": _actor(entry.get("actor")),
    }
    if out_clipped or stderr_clipped:
        shaped["truncated"] = True
    if entry.get("abandoned"):
        shaped["abandoned"] = True

    return {key: value for key, value in shaped.items() if value is not None}


def _history_entry(entry: dict) -> dict:
    """One past command, in the shape os_exec answers with.

    The journal keeps wire fields the daemon needs; an agent reading its own
    history needs the command and what came back, and pays for every other byte.
    """
    if entry.get("kind") == "run_test":

        return _history_run_test_entry(entry)
    if entry.get("kind") != "exec":

        return {"kind": entry.get("kind"), "actor": _actor(entry.get("actor"))}

    result = entry.get("result") or {}
    code, code_clipped = _clip(entry.get("code"), MAX_RESULT)
    stdout, out_clipped = _clip(result.get("stdout"), MAX_STDOUT)
    value, value_clipped = _clip(result.get("result"), MAX_RESULT)
    shaped = {
        "n": entry.get("ordinal"),
        "code": code,
        "status": entry.get("status"),
        "stdout": stdout or None,
        "result": value,
        "error": result.get("error"),
        "duration": round(result["duration"], 3) if result.get("duration") is not None else None,
        "actor": _actor(entry.get("actor")),
    }
    if code_clipped or out_clipped or value_clipped or result.get("stdout_truncated"):
        shaped["truncated"] = True
    if entry.get("abandoned"):
        shaped["abandoned"] = True

    return {key: value for key, value in shaped.items() if value is not None}


@mcp.tool(
    description="Recent commands and results of a session, rebuilt from its journal.",
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_history(session_id: str, limit: int = 20) -> Any:
    feed = await _call("GET", f"/api/sessions/{session_id}/history")
    if feed.get("error"):

        return feed

    meta = feed.get("session") or {}
    entries = [_history_entry(entry) for entry in (feed.get("entries") or [])[-limit:]]
    session = {
        key: meta.get(key)
        for key in (
            "session_id", "container", "database", "owner", "state",
            "allow_commit", "commands", "committed", "unmasked",
        )
    }
    # A closed session still hands back its transcript, so this is the only
    # place the refusal would otherwise be lost: `_call` only surfaces
    # `session_gone` out of an error status, and this one arrives with a 200.
    if meta.get("gone"):
        session["gone"] = meta["gone"]

    return {
        "session": session,
        "entries": entries,
        "journal": f"/api/journals/{session_id}",
    }


@mcp.tool(
    description=(
        "The transcript of a session, live or long finished — every command, "
        "result and line Odoo logged, in order. fmt='markdown' (default) or "
        "'jsonl' for the raw records. A long one comes back a page at a time: "
        "the end by default, any other stretch with first= and last= (1-based "
        "line numbers, inclusive), with total_lines to say how much there is."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_journal(
    session_id: str,
    fmt: str = "markdown",
    first: int | None = None,
    last: int | None = None,
) -> Any:
    exported = await _call("GET", f"/api/journals/{session_id}", params={"fmt": fmt})
    if "text" not in exported:

        return exported

    return _journal_page(session_id, fmt, exported["text"], first, last)


def _journal_page(
    session_id: str, fmt: str, text: str, first: int | None, last: int | None
) -> dict:
    """One stretch of a transcript, within `MAX_JOURNAL` characters.

    The whole of it when it fits. Otherwise the end, which is where a run
    says how it went — or the range asked for, cut short where the budget
    runs out. A single line longer than the budget (a record carrying a
    megabyte of stdout) is clipped rather than refused.
    """
    lines = text.splitlines()
    total = len(lines)
    if first is None and last is None:
        if len(text) <= MAX_JOURNAL:
            start, stop = 0, total
        else:
            start, stop, size = total, total, 0
            while start > 0 and size + len(lines[start - 1]) + 1 <= MAX_JOURNAL:
                start -= 1
                size += len(lines[start]) + 1
            start = min(start, max(total - 1, 0))
    else:
        start = max((first or 1) - 1, 0)
        wanted = min(last or total, total)
        stop, size = start, 0
        while stop < wanted and size + len(lines[stop]) + 1 <= MAX_JOURNAL:
            size += len(lines[stop]) + 1
            stop += 1
        stop = max(stop, min(start + 1, wanted))
    page = "\n".join(lines[start:stop])
    clipped = len(page) > MAX_JOURNAL
    if clipped:
        page = page[:MAX_JOURNAL] if first is not None or last is not None else page[-MAX_JOURNAL:]
    answer = {
        "session_id": session_id,
        "fmt": fmt,
        "total_lines": total,
        "first": start + 1 if stop > start else None,
        "last": stop if stop > start else None,
        "text": page,
        "truncated": clipped or start > 0 or stop < total,
    }
    if answer["truncated"]:
        answer["recovery"] = (
            f"{total} lines in all; pass first= and last= (1-based, inclusive) "
            "to read another stretch"
        )

    return answer


def main() -> None:
    logging.basicConfig(level=logging.INFO)  # stderr, never stdout
    mcp.run()


if __name__ == "__main__":
    main()
