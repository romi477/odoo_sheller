"""What the daemon tells an agent, for whichever client asks.

An MCP client reads these through os_help and the server's instructions; an
HTTP client reads them from /api/help. The two name their tools differently,
so a text never spells one: it writes `{{exec}}`, and each surface puts its
own name there — `render_for_mcp` the `os_` one, an HTTP client its own.
Plain text, no state: what was already handed over is the MCP server's
bookkeeping, not this module's.
"""

import re

from odoo_sheller import rules

# The tool names a text may write as `{{name}}`: the MCP tools, without their
# `os_` prefix. Anything else in braces is a typo a test catches.
MARKERS = (
    "exec",
    "source",
    "journal",
    "history",
    "run_test",
    "test_result",
    "list_tests",
    "open_session",
    "close_session",
    "commit",
    "rollback",
    "interrupt",
    "session",
    "attach_session",
    "help",
    "list_sessions",
    "list_containers",
)

_MARKER = re.compile(r"\{\{(\w+)\}\}")

# The MCP tools are these names behind this prefix.
MCP_PREFIX = "os_"


def render_for_mcp(text: str) -> str:
    """The text as an MCP client reads it: every marker its `os_` tool."""

    return _MARKER.sub(
        lambda match: MCP_PREFIX + match.group(1) if match.group(1) in MARKERS else match.group(0),
        text,
    )


INSTRUCTIONS = """\
odoo-sheller runs Python in a live Odoo shell. `env` and `self` are Odoo's
own, and variables persist between commands: a session is a workspace, not a
series of scripts.

Hosts cut this at 2048 characters. The rest is behind {{help}}(topic):

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
  odoo_<major>   Python and XML for that Odoo; {{open_session}} hands it over

Read the topic before working around something. Not negotiable:

- One command at a time per session; a second is `session_busy`, never
  queued. Wait, or stop it with {{interrupt}}.
- One session at a time: {{close_session}} the one you hold before opening
  another. After editing project Python open a fresh one — the running one
  imported the old code.
- Rollback is the default. Nothing persists until a commit; close, kill or
  death discard it. End experiments with {{rollback}}.
- Commit is a right the human grants. On `commit_not_allowed`: stop, say
  what you want to write and why, then poll {{session}} until allow_commit
  is true, not {{commit}}. `commit_forbidden` is never granted to you (an
  agent does not write to production): read, roll back, say what to write.
  Never env.cr.commit() or env.cr.rollback() in code: each has a tool.
- Work only in a session you opened or were handed. `not_owner` means ask
  for a handover, not a second session; never use a key you were not given.
- Never touch ~/.odoo-sheller/ and never call the daemon's admin endpoints.
- Read Odoo source with {{source}}, never with docker exec: {{help}}('code').
"""

HELP: dict[str, str] = {
    "sessions": """\
## Sessions

Open one with {{open_session}}, or adopt one the human hands you with
{{attach_session}}(session_id, write_key). You will normally be told both in a
message: the id alone is public and grants nothing, the write key is what allows
you to run code there. This server keeps your keys; you never pass them again.

One command runs at a time in a session. A second one is refused with
`session_busy` rather than queued — wait for the first to finish, or stop it with
{{interrupt}}.

{{exec}} waits about 40 seconds for an answer, because the host cuts a call off
not long after. A command still running then comes back as
`request_timed_out`, and it has not been stopped: it runs on, and the session
stays busy, until it ends or the daemon interrupts it at five minutes. Do not
run it again. {{history}} says when it finished and what it returned;
{{interrupt}} stops it now.

If a session is gone (`session_gone`), its process, namespace and variables died
with it. Nothing is restored by reopening. The refusal carries the target and a
journal link; open a replacement with {{open_session}}(replace=<old id>) and
decide for yourself which earlier commands are safe to run again — some of them
wrote to the database, and some were never meant to run twice. {{history}} is the
one tool a dead session still answers: it returns the transcript, and says so in
`session.state` and `session.gone`. Read the transcript, treat the session as
over.

A session may stay open for as long as you have more steps to run in it —
that is what a workspace is. But hold one at a time: when you move on to the
next piece of work, close the one you have with {{close_session}} before
opening another. An idle session still holds a process inside the container,
and a pile of them is the usual way this tool is left in a mess.
{{list_sessions}} shows what you are holding under `yours`; keep that list at
one. Closing discards uncommitted work, the same as rollback.

Editing the project's Python is the other reason to close. The interpreter
imported those files when the session started, and nothing makes it read them
again — not an upgrade, not a rollback, not a new command. A session that was
already open when you changed a `.py` is running the old code whatever the
file on disk now says, so every result it gives you is about code that no
longer exists. Close it and open a new one. Data, views and schema are the
opposite case: those an upgrade does pick up, without a new session —
{{help}}('modules').
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
with {{rollback}}.

Writing to the database is a separate, granted right, and it gates {{commit}}
only — running code is never gated by it. {{exec}} always works once you own a
ready session; the only question is whether persisting it is allowed yet.

Your sessions usually open with the right off, and {{commit}} answers
`commit_not_allowed` until it is granted. The first time that happens:

1. Stop. Do not retry {{commit}} — spinning on it will not see the grant.
2. Tell the human, in plain words, what you want to write and why: which records,
   how many, and what would be wrong if it were rolled back instead.
3. They grant the right in the UI (session header keyboard, Grant commit).
   You will not be told in chat: call {{session}} and look at allow_commit.
   Repeat until it is true, then call {{commit}}.
4. Do not ask them whether they have granted it. {{session}} is how you know.

Once granted, the right stays granted — call {{commit}} directly for any later
commit in this same session, with no need to repeat this ritual first. It
lasts until the human revokes it, and does not survive a handover: check
again after one, the same way as the first time.

A human may also turn on "agents may commit" for a local container, in the
UI. Every agent session on that container then opens with the right, and
{{session}} shows `commit_grant: "container"` — the human's standing grant —
where a grant made in that one session reads `"session"`. They can turn it off
at any moment, and the right goes with it mid-session: on `commit_not_allowed`
go back to the steps above. It never applies to a remote target.

Uncommitted work travels with a session when ownership moves, and this is not
left to your memory: {{session}} reports `inherited_pending`, and the first
{{commit}} on such a session is refused as `inherited_pending` with the count.
Say whose work is in the transaction and what you are about to write, then
call {{commit}}(include_inherited=True) — or {{rollback}} to discard all of it.
Any commit or rollback clears the count; after that the transaction is yours
alone.

Both boundaries are tools, and only tools. Never write `env.cr.commit()` or
`env.cr.rollback()` in code you hand to {{exec}}. {{commit}} is not a wrapper
around `cr.commit()`: it is `flush_all()`, then `cr.commit()`, then
`invalidate_all(flush=False)`, and {{rollback}} is `invalidate_all(flush=False)`
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
{{commit}} remains the only thing in this tool that writes.
""",
    "remote_server": """\
## Connecting to a remote Odoo server

A human may hand you a session that runs on a remote instance rather than a
local container — an odoo.sh build or a server reached by ssh, say. You cannot
open one yourself: no tool here takes a host, a build or a card, on purpose. You only ever receive one. That
includes reopening a dead one: {{open_session}}(replace=...) on a session that
ran remotely is refused and says so, because a journal records which instance it
was but not how to reach it. Ask for a handover rather than a way around.

Two things differ there, and {{session}} shows both as `kind` and `stage`:

- Commit is off for everyone until granted, not only for you. Locally a human
  owner may commit at will because they confirm in the UI; on someone's own
  Odoo that is not enough, so they grant it there the same way they grant it
  to you.
- On a `production` instance an agent never writes, and the refusal is
  `commit_forbidden` rather than `commit_not_allowed`. The difference matters:
  `commit_not_allowed` means ask the human and then poll {{session}}, while
  `commit_forbidden` means nothing will ever grant it to you. Polling for that
  grant is an endless loop. Do what you can without writing, end with
  {{rollback}}, and say what should be written and why. A human may take the
  session back and commit it themselves: they type the instance's name, and the
  grant is for one commit — so the work you hand over is checked by someone who
  commits it, or not at all.
""",
    "limits": """\
## What you cannot do, and must not attempt

- Grant yourself commit rights, or call the daemon's admin endpoints.
- Take a session you were not handed, or use a key you were not given.
- Read or write ~/.odoo-sheller/ directly. Everything you need is in these tools.
- Count on Odoo 13 or 14. They open, with a warning, but are not fully tested:
  sessions, exec, commit, rollback and interrupt should work; {{run_test}} does
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

    {{source}}(path="integration_shopify")              # what files exist
    {{source}}(path="integration_shopify/models/external/external_payout.py",
              first=363, last=470)                     # 1-based, inclusive
    {{source}}(model="account.move", method="action_post")
    {{source}}(model="account.move", method="action_post", module="account")

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
a class, comparing two overrides — and all of it goes in {{exec}}.

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

When the log is what you are after, ask for it: `{{exec}}(code, stderr=True)`
returns `stderr`, the lines in order, clipped from the end because the last
one usually says what happened. `stderr_truncated` means the daemon dropped
whole lines past its own ceiling; `truncated` means this server clipped
characters.

If `stderr_lines` was non-zero and you decide afterwards that you need the
lines, do **not** run the command again — it may have written to the
database, and a second run is a second write. Read them from the journal:
`{{journal}}(session_id)` has every line, interleaved with the commands by
time, including whatever a response clipped and including a traceback Odoo
logged rather than raised. `fmt="jsonl"` gives the raw records instead of
the transcript. A long journal comes back a page at a time — the end of it
by default, any other stretch with `first=` and `last=` (1-based line
numbers, as in {{source}}) — and says how many lines it has in all. Never
install a logging handler of your own to capture the log; it is collected
for you either way.

`{{run_test}}` on a class or a method returns its `stderr` without being
asked, because there the log *is* the answer — which test failed and why. A
whole module answers with `stderr_lines` instead and leaves its log in the
journal: {{help}}('tests').
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
instance — {{session}} reports it as `kind` — where you were lent access, not
given the database. Never call either one there. Modules that depend on the
one you name are upgraded with it, and installing one installs whatever it
depends on.

This is how the module's migration scripts run, which is usually the point. A
script runs only if its version is above what `ir.module.module.latest_version`
records and no higher than the manifest's, so a database already at the
manifest version redoes schema and data but runs no script.

Edited Python is not picked up: the process imported those files at startup
and an upgrade does not re-import them. For a change in a `.py`, close this
session and open a new one — {{help}}('sessions').
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

{{run_test}} takes three forms:

- `module` — every standard test in that module, at_install and
  post_install, the set `--test-tags /module` runs. Tests tagged `-standard`
  or `external` are left out: name their class to run one of those.
- `module.TestClass` — one class.
- `module.TestClass.test_method` — one method.

`params` adds what Odoo's tag syntax carries in brackets, `[...]`, which the
test reads as `_test_params` — Hoot's suites take them as filters; see JS
tests below.

Every answer carries `failed`: one entry per failing test, `{"test": spec,
"kind": "failure" | "error"}`, where `test` is ready to pass straight back to
{{run_test}} — with `params` too when the run had them: pass both back, or
a browser test reruns every module's suite. A failed subtest is listed as
its method, a class whose
setUpClass failed as the class, and a test file whose setUpModule failed as
the whole module — no tag can name a file. To find out why one failed, run
that spec on its own: a class or a method answers with its log tail in
`stderr`.

A whole module answers with counts and `failed` only. Its log is thousands
of lines, so it comes back as `stderr_lines`, a count, and stays in the
journal — {{journal}}(session_id) has every line if you really need it. Do
not rerun a module to see its log; rerun the failed specs.

Unlike {{exec}}, it always
opens its own brand-new session first — it never reuses a session you already
have — so there is nothing of yours it can discard. That session closes itself
the moment the run settles: do not call {{close_session}} on it, and do not
count it against yourself in {{list_sessions}}. To run everything a module
has, pass the module rather than calling {{run_test}} once per class.

When the class name is unknown, call {{list_tests}}(module) rather than inventing
names. Do not open a session per method unless a single method is the point.
Do not fire a list of classes as parallel {{run_test}} calls: one module run
covers them in one session.

Odoo's own test runner rolls back whatever transaction a session's cursor is
holding before it runs tests. This only matters if you call {{run_test}}'s
underlying session a second time after using {{exec}} in it — the response's
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
{{run_test}} hands back `{"status": "running", "session_id": ...}` instead —
that is not a failure, and the run is still going in the container.

When you see it, call {{test_result}}(session_id). That waits too, and answers
either with the finished outcome or with `status: "running"` again — in which
case call it again straight away. There is nothing to pause between calls,
and nothing to clean up afterwards: the session closes itself.

Never answer a `status: "running"` by calling {{run_test}} again. That starts
a second, duplicate run on top of the first.

To run a test in a session a human handed you rather than a fresh one, pass
{{run_test}}(session_id=...). On a remote instance that is the only way, since
no tool here can name one. That session is not yours: it does not close
itself, and you do not close it either. Watch `discarded_pending` there — if
the human left work in it, Odoo's own runner rolled that back before testing.
Passing both a session_id and a container is refused rather than guessed at.

### JS tests (Hoot, `/web/tests`)

Hoot runs in the browser against the web process, not in this session, and
the web process caches which files each asset bundle has. A test file you
add, delete or rename stays invisible to it — and a deleted one makes the
whole bundle fail with `FileNotFoundError`, so the test page comes up blank —
until the assets cache is cleared and the other processes are told. On 18
and 19, in {{exec}}:

    env.registry.clear_cache('assets')
    env.registry.signal_changes()

then reload `/web/tests`. The second line is the one that reaches the web
process; the first alone changes only this session. On 20 caches are
signalled on commit instead, so signal on a cursor of your own (it commits
that one row and nothing of your transaction; read from source, not yet run
on a 20):

    with env.registry.cursor() as cr:
        env.registry._signal_changes(cr, ['assets'])

This is not a restart and does not need one. What it cannot reach is the
manifest: each process reads it once, so a new entry or glob in `assets`
needs the human to restart the web process. Ask for that once per module;
after it, new test files under the glob need only the two lines above.

Hoot also runs headless, through {{run_test}}, when the container has
Chromium and `websocket-client` in Odoo's own Python. Name the Python test
that drives it and pass Hoot's filter as `params`:

    {{run_test}}("web.WebSuite.test_unit_desktop", params="@integration", timeout=300)

`params` goes on the test tag in brackets, and Hoot reads it as a filter: a
module (`@integration`), a suite or one test by its id
(`@integration/views/list/renders the badge`), several joined by commas
with no space after them (Odoo splits only on a comma right before an `@`),
a `-` in front to leave one out. A module's tests are in the run only if its
manifest lists them under `web.assets_unit_tests`. With params the default
ceiling is at least a class's 300 seconds, since Chrome alone takes about
ten to start.

Whether the container can run it at all is in {{session}}: `js_tests` has
`available`, and when that is false a `reason` — `chrome_not_found`,
`websocket_client_missing`, or `runner_not_supported` before 18. Odoo skips a
browser test it cannot start rather than failing it; a run that was skipped
answers `success: false`, `error: "js_tests_skipped"` and `hoot.skipped`.
That is not a pass and not a failure of the code: say what the container
lacks instead of rerunning.

A filter Hoot has nothing for does not narrow the run: Hoot drops it, and
with no filter left it runs every JS test it has. The session stops such a
run the moment Hoot says so, and the answer carries `hoot.unknown_filters`.
Check the id — `@module/` then the suite path and the test name as the test
file spells them — and that the file is in `web.assets_unit_tests`.

A run in a fresh session sees every file on disk. One in a session you were
handed reuses that process's asset lists; on 17 to 19 the session drops them
before each run, on 20 it cannot — there, ask for a fresh session before a
rerun that has to see a new file.

The answer's counts are about the one Python test that ran Hoot. `hoot` has
Hoot's own: `passed`, `failed` (summed over every browser test the run
had), `failed_tests` — ids to pass straight back as params to rerun just
those — and `reasons`, each failed id's failed assertions, clipped: the log
tail in `stderr` is the summary and a traceback, not them. That is 18
and 19; on 20 `test_unit_desktop` picks the modules from the run itself, and
that form has not been run here.

Without Chrome or without `websocket-client`, Odoo skips the test:
`Chrome executable not found` or `websocket-client module is not installed`.
A skip is not a pass: say the tests did not run.
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


# os_help answers to these as well as to HELP; the text is in odoo_sheller/rules.
RULES_TOPIC = "odoo_{}"


def rules_topic(topic: str) -> str | None:
    prefix = RULES_TOPIC.format("")
    if not topic.startswith(prefix) or not topic[len(prefix):].isdigit():

        return None

    return rules.for_major(int(topic[len(prefix):]))


def topic_names() -> list[str]:
    """Every topic once: HELP's, then one per major that has rules. No alias."""

    return list(HELP) + [RULES_TOPIC.format(major) for major in rules.majors()]


def text_of(topic: str) -> tuple[str, str | None]:
    """The topic an alias stands for, and its text — None when there is none."""
    topic = HELP_ALIASES.get(topic, topic)

    return topic, HELP.get(topic) or rules_topic(topic)
