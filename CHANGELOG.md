# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.9.0] — 2026-10-07

An Odoo installed on a server becomes a third kind of target, written down on a
card by whoever can log in; Odoo 13 and 14 open, with a warning. **Breaking:** an
odoo.sh build is no longer named in the request that opens a session
(`build`/`host`/`kind` are gone from `POST /api/sessions`; open a card by
`target_id`), and `POST /api/probe/odoosh` is replaced by the admin-gated
`POST /api/targets/probe`. Nothing is added to the MCP surface: an agent still
never opens a remote target. Also in this release, from the 1.8.4 patch below: the
daemon refuses a request not addressed to it, and a build or host that is not a
name never reaches ssh.

### Remote instances are cards a human wrote down — **breaking**

An odoo.sh build is no longer named in the request that opens a session.
`build`, `host` and `kind` are gone from the body of `POST /api/sessions`: a
session opens a card by `target_id` (`odoosh-<build>`), with no compatibility
shim — the only known third-party client opens local containers only. And
`POST /api/probe/odoosh`, which took any build and host from anyone who could
reach the port, is gone: probing runs on a card, behind the admin key, as
`POST /api/targets/probe`.

The cards live in the daemon, in `~/.odoo-sheller/targets.json` (one JSON file,
one key per kind of target, written atomically, mode 0600, left untouched when
it cannot be read), where the page used to keep them in `localStorage` — which
a desktop-app frame does not reliably keep. `GET /api/targets` lists them with
no key, since they are names and not secrets; `POST`, `PUT` and `DELETE
/api/targets` and the probe need the admin key, because a card is an instruction
to this machine to open an ssh connection somewhere, and only a human gives one.
The UI asks for the key the first time one of those is refused, and moves what
the browser had kept to the daemon, once, when the odoo.sh list is first
opened. A card that has not been probed since the page loaded can still be
opened: the daemon asks the instance on the way in and refuses there if it must.

A session opened from a card describes itself with that card's `target_id`
(`null` for a container), and the page matches sessions to cards by it: a
server's name is free text, so it can equal a container's, and it can be
changed while a session is live.

### Only a human opens a remote target — and the daemon says so

Until now this held by omission: no MCP tool takes a host, a build or a card. That
stops an agent that uses the tools and nothing that can reach the port — `curl` to
`127.0.0.1` with a card id read from the open list would have opened a production
server. `POST /api/sessions` that names a card (`target_id`) is now `403 needs_a_human`
unless the request came from the page the daemon serves — a browser puts that page's
`Origin` on it, which the Host/Origin guard has already checked is the daemon's own —
or carries the admin key. The person at the UI is the admin already and types nothing;
a bare `curl`, `requests` or `httpx` puts no `Origin` and is refused before the card is
looked up, so it learns nothing, not even that the card exists. A local container still
opens without either. This is not a boundary against a script that adds the header: the
daemon cannot tell a person's click from one that imitates it (`docs/security.md` says
what would).

### An Odoo installed on a server, reached by ssh

A third kind of target next to a local container and an odoo.sh build: an Odoo
installed directly on a server. The daemon cannot know which user runs it, which
interpreter (a venv, usually), where `odoo-bin` and its config are, or which
database — so it does not guess. The person who can log in writes a card, in
two fields: **Access**, how to arrive as the right user, and **Launch**, what to
run once there.

    Access   ssh -i ~/.ssh/acme.pem ubuntu@acme.example.com sudo -n -u odoo -H
    Launch   /opt/odoo/env/bin/python /opt/odoo/odoo-bin shell -c /opt/odoo/odoo.conf

Access is a *prefix*, and has to be: `sudo` closes every file descriptor above
2, and the transport rests on `exec 3<&0` keeping the command pipe alive, so
the script that does it runs inside the user switch — checked on a real server,
and by a test that puts a real `sudo` in the chain. Steps typed one after
another into a terminal (`ssh`, then `sudo su`, then `su odoo`) nest; the
equivalent is one line.

Both fields are data, not code. They are tokenised, checked against a
whitelist — not a blacklist — and every token is quoted again on the way out;
nothing a person typed reaches a script or a local shell. Access accepts `-i`,
`-p`, `-l`, `-J` and `-o` with `Port`, `IdentityFile`, `User`, `ProxyJump`,
`ConnectTimeout` and `IdentitiesOnly`, and refuses what would make ssh do
something on *this* machine (`ProxyCommand`, `LocalCommand`, forwarding, `-F`,
a pty) and anything that would weaken host key checking, with a message that
says why; options after the host are accepted and normalised. After the
destination only `sudo -n [-u USER] [-H]` may follow — `su`, `runuser` and
`doas` are refused until someone has checked that they keep fd 3. Launch is
run exactly as written: nothing is appended but `-d DATABASE`, and only when
the optional Database field is filled; writing it in both places is an error.

Nothing on a plain server says what it is, so **stage is declared on the card**
and defaults to `production`, which refuses a commit outright. `staging` and
`development` behave like an odoo.sh staging build: commit is off until a human
grants it, even for the owner. A probe (`POST /api/targets/probe`) shows who the
recipe lands as, where, which interpreter, whether the executable and the config
are readable by that user — a server's config is usually readable only by its
Odoo user, so forgetting the `sudo` reads as exactly that — and which Odoo, read
from `odoo/release.py` beside `odoo-bin`. When that file is not there the gate
is applied when the session says its version. An ssh failure is reported with
what to do: an untrusted host key is "connect once from a terminal", never
accepted on the daemon's own.

Tested against `tests/ssh_double/`: a container with `sshd`, `sudo` and Odoo 19
installed in the system, started by `tests/ssh_double/up.sh`.

### A server whose config has workers: "Address already in use"

The first real server a card was tried on — Odoo 13, `workers = 4`, the service
running — answered a session with `OSError: [Errno 98] Address already in use` on
port 8069, from the shell, before it had loaded anything; adding `--xmlrpc-port 8068`
made it go away. The source had suggested the opposite: that `shell` starts no HTTP
server, so no `--no-http` was wanted. That is true only for `workers = 0`. With
`workers` above 0, which a server that is in use usually has, Odoo takes the prefork
path, and `PreforkServer.start` binds the HTTP port before the shell starts, then
closes it again after the registry preload. Reproduced on that server with the card as
written (`socket.bind` → `Address already in use`) and fixed by `--no-http` (exit 0,
nothing left running, the service untouched).

The launch is still run exactly as written, so what the person is told is what
changed. The form warns, as the recipe is typed, when the Launch has neither
`--no-http` nor `--workers=0` — and, when it only moves the port (`--xmlrpc-port`,
`--http-port`, `-p`), that this works for as long as nothing listens on the new port,
not that HTTP is off. Probe reads `workers` and `http_port` from the server's config
and says what will happen to this launch, on the card and in the form; it does not
refuse, since it cannot know the service is up. A session that dies on the port keeps
Odoo's words and is told what to add. The Launch placeholder carries `--no-http`.

The double has `workers = 2` and something listening on 8069, so the end-to-end tests
fail the way the real server did; they cover leaving HTTP alone, both ways of turning
it off, a free moved port (twice in a row), and a port moved onto the service's own.

### Odoo 13 and 14 open, with a warning, and are not claimed

The one server this was tried against runs Odoo 13, and the gate refused it.
`UNTESTED_MAJORS = (13, 14)` now lets both through: the probe answers `supported`
with `untested: true` and a `warning`, and a card shows an amber *untested* badge
whose hover is the whole sentence. They are not claimed — nothing has run on either
here, and no document says "supported". What there is is a reading of their source:
`odoo/cli/shell.py` is the same file as 15's, `Environment.clear` and
`BaseModel.flush` are there, so a session, exec, commit, rollback and interrupt
should work; 14's test runner has the shapes the fallback expects; **13's does not
exist** — it keeps its runner in `odoo.modules.module` and has no loader to build
a suite from — so `os_run_test` there answers `TestRunnerUnsupported` instead of an
ImportError, before an HTTP daemon is spawned for a run that cannot happen.
Anything older than 13 is still refused, and the message now names 13 and 14 as
what would be let through. A server's version that the probe could not read is
checked against the same gate when the session says it. The bootstrap is tested to
parse as Python 3.6, the floor 13 declares.

### The Connect screen learns about servers

A third mode beside Local and Odoo.sh: **Server (SSH)**. A form with Name, Access,
Launch, an optional Database and a Stage (default `production`), and under it what
the recipe means, in sentences, as it is typed — decoded by the daemon
(`POST /api/targets/parse`), so the page never interprets a recipe itself. A field
the grammar refuses gets its reason beneath it; a form left without the admin key
shows one line and a button rather than a dialog in the middle of typing. Probe
runs the recipe once; Save writes the card; a card shows where it goes and who it
runs as (`summary`, read from the recipe, so a card is recognisable before it is
probed — and one whose key file has gone says so and stays listed, to be edited),
its stage, and an *untested* badge for 13 and 14. The two kinds of remote card now
draw from one function. Everything a card says is set as text, never as markup.

On the odoo.sh and server screens the form and the saved cards are two regions:
the form is a dashed panel under its own title (it turns amber and says *Editing
‹name›* when a card is changed), and the cards sit under *Saved servers · N* with
a rule. The server form folds to its title, with an arrow, and the choice is kept.
A card under the pointer is outlined in the brand cyan.

## [1.8.4] — 2026-10-07

### The daemon answers only requests addressed to this machine

The API has no authentication, and a page open in the user's browser can
still send it requests. Nothing checked where a request said it was going.
That left two ways a page could reach the daemon without the admin key. A
WebSocket handshake is not subject to CORS, so a page on any site could open
the event streams and read session state and Odoo's stderr, and only the
server could refuse it. And a page on a domain of its own, re-pointed at
`127.0.0.1` (DNS rebinding), is the daemon's own origin as far as the browser
is concerned, so everything CORS would have stopped is open to it — opening a
session included. Neither was seen being used; both are how a local service
without a login gets reached, and the next change in this line makes a wrong
answer here cost more (a recipe that names a command to run on the machine).

Every HTTP route and both WebSocket routes now require the `Host` to be this
machine — `127.0.0.1`, `localhost` or `[::1]`, on any port, since a `-p`
mapping moves the port and never the name — and, when the request carries an
`Origin`, that it be the daemon's own: the same authority as the `Host`, not
merely some loopback one, because a dev server on another local port is a
different page. `0.0.0.0` is refused, as is a missing or repeated `Host`.
The UI, in a browser or in the desktop app's frame, is served by the daemon
and satisfies this unchanged; the MCP server, the app's supervisor and `curl`
send no `Origin`. A refusal is `403` with `foreign_host` or `foreign_origin`;
a socket is refused at the handshake.

A client that reaches the daemon by another name — `host.docker.internal`
from a second container, a reverse proxy — now gets that `403`. The daemon
was never meant to be reached that way (`docs/security.md`).

### A build and a host are names

`build` and `host` — the two fields that identify an odoo.sh instance — went
into an `ssh` argument as typed. `ssh` reads an argument beginning with `-`
as an option, and `-oProxyCommand=…@host` is an option that runs a command on
the machine running the daemon. It did not run only because the next argument
held a space and broke the hostname, which is luck, not a guard. Both are now
letters, digits, `.`, `_` and `-`, beginning with a letter or a digit, or the
request is `422` and nothing is started; the same check sits in
`transport.py`, the one place either reaches `ssh`; and every `ssh` command
now ends its options with `--` before the destination.

## [1.8.3] — 2026-10-03

### A whole module, and only what failed

`os_run_test` took a class or a method, so testing a module meant one call
per class — or a log of thousands of lines nobody wanted to read. It now
also takes a bare module, `os_run_test("integration_prestashop")`, and runs
that module's standard tests, at_install and post_install, through the same
`run_tests` call in the same throwaway session: nothing is installed or
upgraded, and a database counted before and after reads the same. Standard
means the set `--test-tags /module` runs — the bootstrap tags `/module`, not
`*/module`, so tests tagged `-standard` or `external`, the ones that call
real third-party services, still need their class named.

Every run now answers with `failed`: each failing test as a spec to pass
straight back to `os_run_test`, read off Odoo's own `FAIL:` and `ERROR:`
lines as they happen. A failed subtest is listed as its method, a class
whose `setUpClass` failed as the class, and a test file whose `setUpModule`
failed as the whole module, since no tag can name a file. Odoo logs those
fixture failures under the suite — `odoo.tests.suite`, `unittest.suite` in 15
— rather than under the test, and the parser reads both; reading only the
test's own logger, a class that could not set up was counted nowhere and a
module run answered `errors: 1, failed: []`. A module run sends its log as a
count, `stderr_lines`, and leaves the lines in the journal; a class or a
method still sends its log tail, since that is where the reason is. The
list is journalled with the result, so `os_test_result` returns it too, and
the MCP instructions describe all three forms.

Without a `timeout`, `os_run_test`'s ceiling now follows the spec: 30 seconds
for a method, 300 for a class, 1800 for a module. One default of 30 stopped a
whole module unless the agent remembered to say otherwise — the first module
tried took 28.9 seconds.

Reading those lines turned up a bug in the test-progress parser from 1.8.2:
a one-line `ERROR:` carries Odoo's perf_info after the test name, and the
name was taken with it. The patterns strip it now.

`/health` read the version from package metadata on every request. On an
editable install a version bump rewrites that metadata under a daemon still
running the old code, so one started on 1.8.1 code answered `1.8.3` — while
refusing a request without `odoo_bin` that 1.8.2 accepts. The version is now
read once, when the daemon starts.

### An interrupt costs the command, never the session

Interrupt reached the bootstrap through the handler Odoo's shell installs,
which raises `KeyboardInterrupt` wherever the process happens to be. While a
command runs that is the point; anywhere else it was a crash. The `repr` of a
command's value ran outside every handler — a large recordset's takes seconds
and looks hung, which is exactly when someone presses Interrupt — and the
interrupt ended the process, its namespace and its open transaction. Landing
while a result frame was being written, it cut the frame in half: the daemon
could not read the line, and the session sat busy until its timeout. The
bootstrap now raises on `SIGINT` only while a command runs — its code, the
`repr` of its value, a test run, a commit or a rollback — and ignores it
everywhere else. An interrupted `repr` reads `<unrepresentable T:
KeyboardInterrupt>`, with no error: the command itself had finished.

### A grant is a human's act

Granting commit, and handing a session to a human, both accepted any key that
held the session — the agent's own included. An agent refused a commit could
grant itself the right, or hand the session to "a human", keep the key that
came back and commit as one. Keys are now remembered with the kind of owner
they were issued to, and both acts take a human's key or the admin key; so
does opening an agent's session with `allow_commit: true`. The refusal is
`403 needs_a_human`. `docs/security.md` says what this still does not stop: a
program that claims to be a human when it opens a session of its own.

A remote session handed to an agent and taken back came home able to commit,
with no grant ever made, and on production claimed a right nothing would
honour. A handover never carries a grant now, in either direction: the new
owner starts where anyone of their kind starts on that target.

### Journals: one file to find one, and no line can take them all down

A closed session's history, `os_test_result` after a run closed itself, a
`replace`, an export: each summarised every journal on disk to find one
file whose name already said which it was. On a machine with 2,000 journals
and 1.4 GB that was 4.5 seconds a call — on the event loop, where no live
session heard from its process meanwhile. A journal is found by its filename
now, where a session ran is read from the file's head, the journal list
keeps each file's summary until its size or mtime moves, and whatever does
read a whole file runs off the loop.

One cut line — a daemon killed mid-write, a full disk — made every reader
raise: the journal list, every closed session's history and every export
answered 500 until someone found the file. A line that is not a record is
skipped with a warning now.

### Elsewhere

- `os_exec` stops waiting after about 40 seconds and says so honestly: the
  command is still running, the session stays busy until it ends or the
  daemon interrupts it at five minutes, do not run it again. The guidance
  promised a 30-second ceiling that never existed on the daemon's side.
- `os_journal` answers a page at a time — the end by default, any stretch
  with `first=`/`last=`, and `total_lines` — instead of the whole transcript:
  a module run's is near a megabyte. The guidance's `fmt="json"` is
  `fmt="jsonl"`; the other only worked because anything but `markdown` was
  read as JSONL.
- `os_attach_session` checks the key it is given: `GET /api/sessions/{id}`
  with a key reports `key_status` for it. A wrong one used to attach without
  complaint and fail on the first command.
- `os_list_tests` on a remote build answers `not_a_container` and points at
  `os_source`, instead of sending a build id to `docker exec`.
- A session whose process dies before `hello` says why: `410
  session_did_not_start` with the last lines Odoo logged. It said `process
  ended`, which left an agent — which does not see the startup log — with
  nothing to act on.
- Closing a busy session no longer reports it `ready` on the way to `closed`,
  which also took a live test run's card away while the run went on.
- The late result of an abandoned command is booked as if it had come in
  time: an `exec` that ran past its ceiling counts as pending work, and a
  commit that finally got its lock is journalled as a commit, `late: true`.
- `exec` takes `read_only: true`, the caller's word that the code writes
  nothing; `os_source` uses it, so a source read no longer makes a handover
  warn or a test run report work discarded. A failed commit no longer marks a
  journal `committed`, and the Markdown transcript keeps an error's message
  when there is no traceback — a timeout, a refused test runner — instead of
  an empty block.
- A WebSocket that falls 10,000 events behind is hung up on rather than
  buffered for as long as it stays connected, and closing a session hangs up
  its sockets.
- Discovery commands and signal delivery are bounded: a paused container, a
  wedged Engine or a dead SSH link used to hang the Connect screen, an
  agent's call or Interrupt with no end. A `docker` or `ssh` that is not
  installed where the daemon runs is an answer naming it, not a 500 — the
  container image has no `ssh`, and `docs/container.md` now says it cannot
  reach odoo.sh builds.
- The admin key is printed only to a terminal. Into the desktop app's
  `daemon.log`, `docker logs` or a `nohup` redirect the daemon writes where
  the key is kept instead. The key file is created `0600` in one step rather
  than written first and narrowed after.
- The journal list puts only the owner kinds it knows into a class
  attribute; a journal from before the kinds were closed could carry any
  string there, unescaped.
- In the desktop app, a paste into a terminal tab running a raw-mode program
  that is not reading — vim, ssh — froze the whole app, Quit included:
  `pty_write` wrote on the main thread, holding the hub's lock, and such a
  write waits from the first byte. Input goes to a writer thread per tab
  now. Quitting beside a daemon the app did not start still asks about open
  terminal tabs, which die with the app either way.

## [1.8.2] — 2026-10-01

### A test run says where it is

A test run used to be a rose badge, a blinking lamp, and an empty feed that
said to go and read the log. The log runs too fast to read, so nobody could
tell which test was running or whether anything had failed yet until the
whole class was done.

Odoo already says it: one `Starting X ...` line per test, `FAIL:` and
`ERROR:` lines as they happen, all from the test module's own logger and the
same in 15 through 20. The session now counts those lines while a
`run_test` holds it, and `describe()` and a new `test_progress` WebSocket
event carry the result: the test asked for, the one running now, and how
many have started, failed, errored and been skipped. The feed shows that as
a card above the cells, with a clock, and drops it when the result lands.

### odoo_bin is found, not asked for

An agent that ran `os_run_test` with a container and a database got
`http_422` back, and the message listed odoo_bin among the required fields
as though it were a choice. It is not: it is a fact about the container,
which the agent could only learn by calling `os_list_containers` and probing
every container on the machine. `POST /api/sessions` now probes the one
container it was given when the path is left out, and refuses an
unsupported or unreadable one before spawning anything. `os_open_session`
and `os_run_test` get it for free. A missing container or database is named
on its own instead of in a list.

### Journals say how big they are

Nineteen hundred journals came to 1.4 GB on one machine, and nothing on the
Journals screen said which of them were the weight. Each row now has a
`size` column after `outcome` — records in the file and kilobytes on disk,
`772 / 243 KB` — and a group heading sums its rows. `/api/journals` carries
the two numbers as `lines` and `bytes`; they describe the file, so they stay
out of the session metadata every export carries.

The row's `copy` word is now the same two-sheet glyph the Connect screen
copies a target with, to give the new column its room. It turns into a
green tick when the copy lands, red when it fails.

## [1.8.1] — 2026-09-28

### An owner is a kind and a name

A program that drives this daemon over plain HTTP — a migration tool, say —
opens a session and hands it to its own agent without the browser's help. The
body model for that was a bare `dict`, so `{"kind": "agent"}` was accepted
verbatim and stored with no name at all. What the session then showed was not
a missing name but a broken one: `watching · undefined` in the owner badge,
`by agent (None)` through its own transcript, and `KeyError: 'label'` out of
`os_history` — so an agent could not read the history of the very session it
had just been given.

`owner` is a model now. `kind` is `human` or `agent` and nothing else, since
it decides whether commit is gated and a typo must not pass as a third kind
nobody has heard of. `label` stays optional and becomes the kind when it is
absent: a caller with nothing better to say is not made to invent something,
and `agent` names an agent well enough.

Journals already on disk carry the hole and are never rewritten, so the three
places that read one were taught the same fallback — the transcript, the MCP
actor, and the badge. `_actor` in particular stopped indexing the label, which
is what turned a thin session record into an exception.

## [1.8.0] — 2026-09-27

Less to retype, and less to get wrong.

Nothing here changes the API or the wire protocol. What changed is the number
of small things a person or an agent had to do by hand, or had to know without
being told: a target retyped into a prompt, a trip from one corner of a card to
the other, a window that would not tile, a script whose size was invisible
until it was opened, and four rules agents broke because the instructions left
them to be inferred.

### The agent instructions say what agents kept getting wrong

Five things that agents did wrong in practice, each now stated where the
mistake happens rather than left to be inferred.

A session that was open before its project's Python changed is running the
old code, and nothing in it ever re-reads the file: not an upgrade, not a
rollback, not a new command. Close it and open a new one. And hold one
session at a time — a new piece of work is a reason to close the old session,
not a reason to open a second one beside it. Both rules are in the delivered
instructions rather than only in `os_help('sessions')`, because a rule that
has to be fetched cannot stop the mistake that keeps it from being fetched.

`env.cr.commit()` and `env.cr.rollback()` are now refused as guidance
outright. `os_commit` is `flush_all()`, `cr.commit()`,
`invalidate_all(flush=False)`; a bare `cr.commit()` in exec'd code skips both
halves, leaves `env` holding stale values, and draws no boundary in the
journal — a write to a real database the watching human never sees happen.
`env.cr.savepoint()` is the transaction primitive that does belong in code.

Upgrading, installing or migrating a module locally no longer asks the human
first. The agent changed the code, so the database has to catch up: the
upgrade is the consequence of its own edit, not a decision to put to someone.
The gate stays exactly where it was earned — a session running on someone
else's instance, where access was lent and the database was not.

`os_help('orm')` gains `env.ref`, and the `remote` topic is now
`remote_server`, since the bare word read as a way of working rather than as
someone else's machine. `remote` still answers, as an alias.

### The write key is shown with one button

Handing a session to an agent shows the write key once and never again. The
dialog that showed it had Cancel and OK — but by then the handover had already
happened: ownership moved on the request above it, and the dialog only
displayed the result. Cancel undid nothing. It threw away the only copy of the
key, leaving a session handed to an agent it could no longer be given to, with
Take back and a second handover as the way out.

There is no "no" to give, so there is one button. `Copy` copies and closes.
Esc copies too — it closes a `<dialog>` on the platform's own terms and no
markup takes that away, and a clipboard that changes unasked is a smaller
surprise than a key that is gone for good. The field is read-only and
pre-selected, scrolled to the front where the session id is, so Cmd+C still
works where the clipboard API is refused.

A handover no longer asks who it is for, either. The prompt defaulted to
`claude` and the default was taken every time; the label is now the constant
`agent`, which is what the owner badge and the journal record. What that
dialog also carried — the warning that uncommitted commands stay in the
session and become part of what the agent could commit — is kept, as a
confirm rather than a field to type in, and only when there is something
uncommitted to warn about. So the ordinary handover is one click and one
dialog: the key.

### A target you can hand over without typing it

The container card's picker gains a `copy` at the far edge, opposite `Start`.
It puts the target on the clipboard as one line of JSON — container, database
and Odoo version — which is what an agent has to be told before it can work:
where to run, against what, and which version's idioms apply. The rest of what
the probe found says nothing about that and stays on the card.

The database is read exactly the way `Start` reads it, so the copy can never
name one that clicking Start would not have used.

### The desktop app gets its Window menu back

macOS window tiling did nothing in this app — fn+Control+arrow, the halves and
quarters every other window obeys — and it was not because the app is a
WebView. The custom menu replaces the standard one whole, and naming a submenu
"Window" does not make it the Window menu: AppKit fills that one itself, with
the window list, Bring All to Front and the Move & Resize items those
shortcuts are bound to, but only for the submenu handed to it. The app never
handed it over, so the keys reached no menu item. One call,
`set_as_windows_menu_for_nsapp`, and all of it is there.

**Window → Compact Width** (Option+Cmd+C) adds the one geometry macOS does not
offer: 38% of the display's usable width, full height, against the left edge —
a column to work in beside an editor. Pressed again on a window already parked
there it crosses to the right edge, so one item reaches both sides. It is a
menu item and not a button in the page's header because that header is the
framed daemon UI, a remote origin that has no Tauri commands and is not going
to be given any.

A share of a display stops meaning anything once it passes what the page can
fill, so it is capped at the canvas: `.app` is `width: min(1180px, 100%)` and
centred, inside the body's 24px and the 1px border of `.shell`. On a 4K panel
38% is 1459px and the last 229 of them were the window's own empty margin —
the window now stops at 1230, measured in CSS pixels so the display's scale
factor applies. A test reads both the stylesheet and the Rust constant, since
nothing else ties them together.

### A container card you do not have to cross

**Open session** sat in the card's action column and **Start** at the other end
of the row below it, so opening a session was a trip from one corner to the
other. They are one control now: clicking **Open session** turns it into
**Start** in the same place, and the second click lands where the first did.
What stays below is the target rather than an action — which database, and the
**copy** beside it — a faint two-sheet glyph rather than a word, since a word
in that corner read as a third key beside **Start** and **probe**. It turns
into a green check for the moment after a copy. Both glyphs ship in the markup
and the stylesheet picks one, so nothing can throw the icon away by rewriting
the button's text.

The card also stopped stacking itself on a narrow desktop window. Two
`width: 100%` rules under the 760px breakpoint forced the name onto its own
line and the database select onto another, whether or not the room was short.
At 684px — the width **Compact Width** gives — the row needs 241 of the 626 it
has. Those two rules now wait for 520px, an actual phone; wrapping itself stays
where it was, since it costs nothing until something genuinely overflows and a
long container name should push the keys down.

### How big was that script, and how much came back

Every cell header now ends with two numbers — lines of code over lines of
output. The output side counts the three pieces **copy output** puts on the
clipboard: stdout, the returned value, and any traceback. A trailing `+` means
the daemon clipped it, so a shortened count cannot read as the whole; a command
still running shows a dash rather than a zero.

Cells an agent wrote arrive folded, which is exactly when this matters: the
size of a script was otherwise invisible until you opened it. The figures are
tabular, so a column of folded cells lines up and can be compared without being
read one by one.

Redrawing the feed is gated on a signature of what each cell shows, and that
signature was missing `result_truncated` — which `resultHtml` had been reading
all along. Added, so a change in either truncation flag redraws.

## [1.7.0] — 2026-09-20

A daemon you do not have to install.

### The daemon as a container

Until now a daemon meant a checkout with a virtualenv, or the macOS app. Both
assume somebody is willing to install something. This adds a third way that
assumes nothing: an image that mounts the host's `docker.sock` and drives the
same containers a native daemon would. No Engine inside it —
Docker-outside-of-Docker — and nothing below `transport.py` notices, because
`docker exec` is `docker exec` whichever side of a container the CLI runs on.

State is a bind mount of the host's `~/.odoo-sheller`, never a named volume.
That is the point rather than a detail: journals have to land where a natively
installed daemon writes and reads them, or the history forks by how the daemon
happened to be started that day. `HOME=/data` is the whole mechanism — every
state path in the daemon is built from `Path.home()`, so one variable moves the
journals, the admin key and the SSH control sockets at once.

The entrypoint resolves two identities that are not interchangeable: the mount's
owner, who must own the journals, and the socket's group, which decides who
reaches the Engine. The first becomes the process, the second is added on top of
it as a supplementary group.

The MCP server travels in the image and nothing starts it. When a daemon was
brought up this way and nothing else odoo-sheller-related is on the host,
`docker exec -i odoo-sheller python -m odoo_sheller.mcp` is how a human still
reaches a stuck module with an agent.

### A contract, not an example

`docs/container.md` is written for a program that starts and stops this
container rather than for a person copying a command. The image carries
`io.github.romi477.odoo-sheller`, so a caller finds what it started — or finds that
one is already running — without remembering a name, and stops it the same way.
The `HEALTHCHECK` is what to wait on instead of polling the port. Failures are
exit codes: 1 for a socket that was never mounted, 2 for a state directory that
does not exist with no `ODOO_SHELLER_UID` to own it.

That second one is a refusal on every host, and deliberately so. The entrypoint
runs inside a Linux container and cannot tell what kind of host it is on; on
Linux, starting there would write journals as root that a natively installed
daemon could never append to. One rule for both beats guessing.

### /health answers the question a caller cannot

Over HTTP it makes no difference whether a daemon is native or in a container,
and a caller that found one on the port should stop caring. The exception is the
MCP server: it is spawned rather than called, and a containerized daemon's has
to be spawned inside that container — which only that daemon knows the id of.

So `/health` now carries `container` and `mcp`, the second being a command to
run as given. A container answers with `docker exec -i <its own id> python -m
odoo_sheller.mcp`, which works whatever name it was started under; a native
daemon answers with its own interpreter. A frozen build with no MCP tree beside
it answers `null` rather than inventing a command that would fail later.

`docs/container.md` gains the procedure this enables: ask `/health`, then the
label, then start one — and a note that skipping it leaves a container behind
in `created` state when the port is taken, which sends the reader after the
wrong problem.

### Elsewhere

- `docs/agent-guide.md` spells the MCP command a fourth way, for a daemon that
  is the image — and says to ask `/health` for it rather than filling in a
  container name by hand. The diagrams in the README, `CLAUDE.md` and
  `docs/architecture.md` stop calling the daemon a macOS process.
- The image's healthcheck probes `/health` rather than `/api/sessions`. The
  dedicated endpoint existed all along; the probe was reaching into the session
  registry to answer a liveness question.
- The MCP server follows `ODOO_SHELLER_PORT` when no `ODOO_SHELLER_URL` says
  otherwise. It usually runs inside the same container as the daemon, started
  by `docker exec`, and that variable is what moves the listener there — so a
  moved port used to leave every tool reporting `daemon_unreachable` about a
  daemon running two lines away. The image declares the variable, and an exec
  inherits it.
- The daemon's warning about its bind address says something true in both modes
  now. Containerized, binding `0.0.0.0` is mandatory — the container's own
  loopback is not what `-p` reaches — and the boundary is how the port was
  published, so that is what the text names.

## [1.6.4] — 2026-09-18

The window already has the file the dialog kept asking you to paste.

### The admin key in the app

Grant access, Grant commit, and taking a session back from an agent asked for
the admin key every time the framed UI had forgotten it. The key lives in
`~/.odoo-sheller/admin.key` — the daemon writes it before it listens — and the
page stored a copy in the iframe's `localStorage`, which WKWebView treats as a
third-party origin and does not keep. A Reload, a rebuild, a day's work of
granting commit to an agent-opened session: the same paste, again.

The window reads that file after `/health` answers, and hands it to the frame
the same way it already hands screen steps: a `postMessage` from the parent,
never an HTTP endpoint. The page keeps it in memory, not in `localStorage`. A
browser tab still pastes once, because a browser is just another client of the
unauthenticated API and must not be given the key for fetching a page. The MCP
server still never sees it.

The app does not create the file. If it is missing or empty after the daemon is
up, the prompt is still there.

## [1.6.3] — 2026-09-17

The window stops answering its own menu after a while, and a Settings dialog
where two files that are not ours are described rather than written.

### A session left open made the page ignore everything

Not only its buttons: the menu items stopped working too, which is what gave it
away. Every one of those is delivered to the page, and the page's main thread
was never free. `renderSessions()` — twenty-eight call sites, and every state,
owner and policy message among them — rebuilt the whole feed of cells, with
their code and output, **and** every row of an open log panel, from scratch.

Measured on a live session, twelve cells and a log 323 rows long:

| | 20 renders |
|---|---|
| unchanged since the last one | 3 ms |
| rebuilt anyway | 1134 ms |

Fifty-seven milliseconds each, at a fifteenth of the log panel's ceiling. Both
now compare a signature of what is already drawn — the cells and their state,
ownership included, and the log's filter and line count — and return without
touching the DOM when nothing moved. `appendLogLine` keeps the signature
current as it adds a row, so the next render does not undo its work.

### Settings

**Settings…** (⌘,) replaces the single MCP item, with two sections:

- **MCP** — the entry to paste into an agent's config, as before.
- **SSH** — `Host *.odoo.com` / `StrictHostKeyChecking accept-new`, which is
  what lets an odoo.sh build be opened at all: the daemon runs `ssh -T` with no
  terminal, and the first connection to a build dies on the host-key question
  because nobody is there to answer it. That is trust on first use, and
  [docs/security.md](docs/security.md) says so plainly — a host whose key has
  *changed* is still refused.

Nothing there is saved or applied, so the way out says Close. Each block has
its own copy button — the two-sheet glyph, no outline — and nothing is copied
just for opening the window. The dialog is outlined in cyan; amber in this UI
means something is live.

### Elsewhere

- ⌃⇧← / ⌃⇧→ move between session tabs, wrapping, and bring the sessions screen
  with them. The layout is now ⌥⌘ for screens, ⌃⇧ for sessions, ⌃⌘ for the
  terminal dock.
- A cell's header carries the date: `#1 browser 17 Sep 16:36 / 0.00s / done`,
  with the full instant in its tooltip. The journal's feed records the
  timestamp it always wrote, so a restored session keeps its dates.
- **About odoo-sheller** heads the application menu — a sheet in Settings'
  frame rather than the platform's panel, with the version straight from the
  crate, what this app is in two paragraphs, and the daemon's address. The menu
  is in the order macOS has always used it: About, Settings, the hide pair,
  Quit.

## [1.6.2] — 2026-09-17

Interface work, and one thing the Connect screen was getting wrong. No change
to the protocol, the daemon or the session model.

### Containers that cannot host a session

A database container beside the Odoo it serves has no `python3` in it, so the
probe came back with Docker's own OCI paragraph and the card wore it in red,
every time the screen was opened, about a container nobody was ever going to
open a session in. The daemon now says what that is —

```
"error_code": "no_python", "error": "no python3 in this container"
```

— keeping Docker's wording under `error_detail` for whoever is debugging the
image itself, and the same for a container with no `odoo-bin`. The screen folds
those under *N containers are not Odoo images* at the end of the list, grey
rather than red, and remembers whether the fold was opened. Every other probe
failure is still red, because every other one can be acted on.

### The keyboard, and where it works

Shortcuts that belong to the window are menu items now, which is what makes
them work wherever the focus is: macOS offers a key equivalent to the menu
before any web view sees it, and the framed UI is a remote origin that sees a
key only while it holds focus.

| | |
|---|---|
| ⌥⌘← / ⌥⌘→ | previous / next screen — the app forwards the step into the frame |
| ⌃⌘← / ⌃⌘→ | previous / next terminal tab |
| ⌃⌘↑ / ⌃⌘↓ | taller / shorter terminal |
| ⌘W | close the session, or the terminal tab — whichever has focus |

⇧←/→ for the screens is gone, and so is ⌘K for closing a terminal tab: ⌘W was
already doing that from the page itself.

### The look

- The header keeps its outline, in the canvas's own colour rather than a
  cyan-lit one, and without the glow under it. Two framed surfaces in two edge
  colours read as two panels arguing about which is the subject.
- The live screen is underlined in amber — the colour this UI already uses for
  what is live — because cyan against muted grey is a weak signal at 13px.
- The session count is an exponent on the word rather than a number behind a
  separator, and there is none at all when there are no sessions.
- The session tab strip takes the canvas colour instead of the header's.
- The log panel's header is a tab: round on top, square where the log comes out
  of it. Its edge is the ordinary border while it is shut and the brighter one
  while it is open.
- Every cell in the feed gets **edit**, which puts that code back in the editor
  instead of re-running it as it stands, dividers between the four verbs, and
  amber on hover. The row belongs to whoever holds the write key: watching an
  agent's session it is not drawn at all, and taking the session back brings it
  to every cell.
- `re-probe` is `probe`, and the editor's height control is set apart by a
  divider and answers amber.

## [1.6.1] — 2026-09-17

A fix release for what a session left running overnight does to the window.
Nothing in the protocol, the daemon or the session model changed.

### The page stopped answering its buttons

The log panel had no ceiling. `record.logLines` grew for as long as the session
lived, and `renderSessions()` rebuilds that panel from the whole array — on
every state, owner and policy message, and **even while the panel is
collapsed**. A session left open against a container logging at DEBUG reached
sixty thousand lines, so every message rebuilt sixty thousand DOM nodes for
something nobody was looking at. Close and Kill were not broken; the main
thread never got to them.

- The buffer is capped at 5000 lines, trimmed on every path that fills it —
  the live line from the socket, the tail from the API, the merge at startup —
  and the rows are dropped from the front of the panel to match.
- A collapsed panel builds no rows at all.
- **The per-session WebSocket reconnects.** It never did: `close` set a flag and
  that was the end of it, so after a drop the card told yesterday's story —
  state transitions, stderr and the death of the process are delivered there
  and nowhere else. It retries every three seconds like the registry socket,
  and resyncs from `/api/sessions` for the gap it missed. A session the daemon
  no longer lists takes its tab with it.

### Dialogs that answered nothing

Every dialog resolved only on the `<dialog>` element's `close` event. Where
that event does not arrive, the promise stayed pending, the shared queue behind
it never advanced, and every later dialog — the admin key prompt, the message
explaining the failure — was swallowed in silence. The buttons settle the
answer themselves now, `close` is only how Esc arrives, and a dialog that
cannot be shown answers the way Cancel does rather than hanging the page.

### A mistyped admin key was permanent

`ensureAdminKey()` treated any stored key as good enough, so the retry resent
the refused one and never asked again; `closeSession` had a second copy of that
retry which never dropped a refused key either. One typo and Close, Kill and
every handover failed for good, with nothing in the UI to clear it. A refusal
now always asks again, with the refused key back in the field to be corrected,
and the second refusal drops it.

### The look

- The session tab strip is the canvas colour instead of the header's: it sits
  inside the canvas and read as a second header.
- Black in the terminal dock is the framed area alone — where the shell writes.
  The strip the tabs sit on takes the window's own ground, so the dock reads as
  part of the application rather than a black band stuck to its bottom.
- The terminal keeps a small margin from its frame, and the row that does not
  fit whole is dropped rather than sliced by the frame's own clipping — xterm
  fits whole rows into a height whose row is a fractional number of pixels, and
  the last one ended a few pixels past the box.

## [1.6.0] — 2026-09-15

A macOS application, and Odoo 20. The daemon is the same daemon: the app is a
window and a supervisor around it, speaking the one HTTP API everything else
speaks, and a browser tab on 8765 keeps working beside it. Odoo 20 needed one
line in the version gate and one shim in the bootstrap — found by a container,
not by reading the sources.

### The desktop app

`odoo-sheller-app/` builds a macOS `.app`. Design of record:
[docs/desktop-app/architecture.md](docs/desktop-app/architecture.md); the work
and its decisions: [implementation.md](docs/desktop-app/implementation.md).

- **One API, still.** The window holds a local page with the daemon's UI in an
  `<iframe>` — no bundled copy of that UI, nothing proxied, and the frame
  loads `http://127.0.0.1:8765/web` the way a browser tab would.
- **It attaches or it spawns, and never moves off 8765.** A daemon already
  there is used and left alone on quit; a free port gets one of ours; anything
  else on the port is reported and nothing is started.
- **The daemon travels inside the bundle.** Frozen onedir trees at
  `Contents/Resources/odoo-sheller/odoo-sheller` and
  `.../odoo-sheller-mcp/odoo-sheller-mcp`: no Python, no `uv`, no checkout on
  the machine. That first path is also how the daemon runs headless, with no
  window. `pytest -m frozen` is the check that `bootstrap.py` and `web/`
  actually shipped — a build that merely launches proves neither.
- **A terminal dock** at the bottom of the window: tabs of `$SHELL -l`,
  resizable by its top edge, opened from the bar, from **Window → Show
  Terminal**, or with Ctrl+`; ⌘T adds a tab, ⌥⌘←/⌥⌘→ move between them,
  ⌘K or ⌘W closes one. Output is batched so a large
  `cat` does not freeze the UI, closing a tab or quitting reaps the process,
  and the quit dialog counts the tabs it is about to kill. It exists only in
  the app and only in the app's own page — the capability declares no `remote`
  origin, so the framed UI cannot reach it. What that widens:
  [docs/security.md](docs/security.md#the-desktop-terminal).
- **MCP Configuration…** shows the entry to paste into an agent's config —
  both shapes, `mcpServers` and Zed's `context_servers` — with the bundled
  binary's path filled in, each in a monospace block with its own **Copy**,
  and puts the first form on the clipboard. **It writes nothing.** Those files are the user's, they hold servers this app knows
  nothing about, and `~/.claude.json` is live state a running Claude Code
  rewrites.
- The framed UI is asked for as `/web?app=1` and drops its `swagger` link
  there: `/docs` leads out of the UI with no way back in a window that has no
  address bar, and it is the one page served here that needs the network. In a
  browser tab the link is untouched.
- Ad-hoc signed, hardened runtime, **no entitlements at all**, Apple Silicon.
  Not notarized: a downloaded copy is cleared in System Settings → Privacy &
  Security → Open Anyway. `packaging/app.sh` is build, dmg, install.
- `ODOO_SHELLER_DOCKER` overrides the docker CLI path, because an app launched
  from Finder inherits a minimal `PATH` and would otherwise list no containers.

### Odoo 20

- `SUPPORTED_MAJORS` accepts 20. A master container still calls itself 19.5
  and already passes as a 19; the entry is for the day the number changes.
- **Running tests needed a fix.** 20 dropped `httpd` from `ThreadedServer` —
  only `GeventServer` keeps one — while `odoo/tests/shell.py`, byte-identical
  to 19, still reads `server.httpd` to decide whether to spawn a daemon. In
  shell mode that is a threaded server, so `run_tests` died of
  `AttributeError` before a single test ran. The bootstrap now spawns the
  daemon itself and answers that guard; nothing outside `GeventServer`
  dereferences the attribute, so answering it is safe. Reading the sources
  did not find this — a container did.
- Everything else survived untouched, and the two things that did move were
  already feature-detected: `OdooTestResult` keeps its counts in sets now,
  behind the same `failures_count` / `errors_count` names, and the thread's
  `dbname`, which `cli/shell.py` used to set, is set by `Registry.__new__`
  instead.

### Daemon

- `GET /health` answers liveness and the package version, with no session data
  and no admin key. The version call is guarded: a frozen build carries no
  package metadata unless the packaging step asks for it, and liveness must
  not depend on packaging, so it answers `"unknown"` rather than `500`.
- The daemon closes its live sessions when it is asked to stop, so their
  journals end with `session_close` instead of simply stopping. Sessions have
  never been able to outlive the daemon; until now they died without saying
  so, which was survivable while the daemon was stopped by hand and is not
  once quitting an app is how a working day ends.
- `odoo-sheller` and `odoo-sheller-mcp` are installed as commands, so neither
  the daemon nor the agent server has to be spelled as `python -m` in a config
  file or a shell function.

### Fixed

- **The web UI asked nothing inside the desktop app.** `window.confirm`,
  `window.alert` and `window.prompt` do nothing in a WKWebView whose host does
  not implement the WKUIDelegate panels, and this one implements only the
  file-upload and media-permission ones. So Commit went through on a single
  click, "Uncommitted work will be discarded. Continue?" was never asked, nine
  failure messages were swallowed, the admin key could not be pasted, and a
  handover gave the session away without showing the write key — which is
  shown once and never again. In a browser all of it worked, which is why it
  went unnoticed. The page draws its own dialogs now; a test forbids the three
  browser calls.
- Quitting stops the daemon the app started on every path, not only ⌘Q. A Quit
  Apple event — Dock, right-click, Quit — never raises `ExitRequested`, and
  the daemon was left holding 8765 with launchd for a parent.
- `tests/test_e2e.py` ran `base:TestFloatPrecision`, which 20 moved out of
  `addons/base/tests` — a fixture that quietly reports `tests_run: 0` and
  reads as a pass, because `wasSuccessful()` is true of zero tests. It now
  runs `base:TestIrDefault.test_conditions`, which exists under those names in
  all six majors.
- `docs/architecture.md` pinned two facts to the wrong lines: the `sql_db.py`
  column pointed at `Savepoint.__exit__`, which only ever rolls back, instead
  of the cursor `__exit__` that commits on a clean exit, and three of the five
  `service/server.py` numbers were off. Those are the numbers someone
  re-verifying the design would open, so they are now a table measured across
  all six versions.

## [1.5.0] — 2026-09-09

### Reading source

- `os_source` reads Odoo source from inside the running instance, in one
  call. By path: `os_source(path="module/models/thing.py", first=363,
  last=470)` — module-relative or absolute inside an addons directory,
  1-based inclusive, through `odoo.tools.file_open`, so anything outside
  those directories is refused. By model:
  `os_source(model="account.move", method="_post")` — the source of the
  override that actually runs, with `defined_in`, `file`, `line`, and every
  module extending that model in resolution order.
- The reason it exists is ergonomic, not informational. An agent still
  reached for `docker exec ... sed -n '363,470p'` after the guidance against
  it was written, because the guidance sat behind an `os_help` call it had no
  reason to make for what looked like "read a file", and because writing the
  equivalent snippet by hand cost more than the shortcut. A rule only holds
  when following it is the cheaper path.
- A method read lists the modules that **define** it, not everything in the
  MRO: `overrides` is the `super()` chain in resolution order, first the one
  that runs, last the base. Measured on `account.move.action_post` in a real
  database: three modules — `integration:43`, `sale:83`, `account:6179` —
  against fifty-two classes in the MRO. `module=` reads one link instead of
  the winner, and a module that does not define the method comes back as
  `not_in_that_module` with the chain to pick from.
- `os_source(path=<directory>)` lists a module's files with their line counts
  — 266 for `integration_shopify`, bytecode and `__pycache__` dropped. The
  question "what is in this module" comes before "read me line 363", and
  leaving that one to the shell is what keeps the shell a habit.
- Fixed before it shipped, by a live container rather than a test: with no
  `first`/`last` the generated snippet carried `json.dumps(None)` — a
  JavaScript `null`, a `NameError` in Python. Every unit test passed because
  they mock the session and never run the snippet. Two tests now do: one
  compiles it in every argument form, one executes it against a stub
  `odoo.tools`.
- The `code` topic said `file_open` was for "files that are not Python",
  which is untrue — it reads any file inside the addons tree, `.py`
  included; only `filter_ext` narrows it. That sentence is what sent the
  agent to the shell, and it is gone. Verified on a live container: the same
  file, the same line range, and `/etc/passwd` refused.
- The delivered instructions now carry one line about it, since a rule
  nobody reads is not a rule: "Read Odoo source with os_source, never with
  docker exec".
- The snippet leaves no names in the session: one function, imports and
  locals inside it, and it pops its own name before doing any work — so a
  read that raises cleans up too.

### Fixed

- A session tab the daemon no longer has can be dismissed again. Sessions do
  not outlive the daemon, so a restart takes every one of them with it — and
  the tabs stayed: Close answered `404 session_gone` and alerted, Kill
  answered the same and alerted, and nothing on the card could remove it
  short of reloading the page. `session_gone` is the state Close was asking
  for, so it now takes the same path as a successful close.
- Reattaching prunes as well as adopts: a session the daemon does not list is
  dropped from the browser rather than left as a tab that can be neither
  typed into nor closed.

## [1.4.0] — 2026-09-09

### Rules that enforce themselves

Three things an agent could only get right by remembering them. Feedback from
one working through a real task named all three; each is now structural.

- A commit on a session handed over with work already in its transaction is
  refused once as `inherited_pending`, with the count of commands that were
  the previous owner's. Saying it out loud and passing
  `include_inherited=True` goes ahead; `os_rollback` discards the lot. Any
  boundary clears the count, so the transaction is the agent's alone after
  that. `Session` tracks it and `os_session` reports it, so the UI can say
  whose work is pending too.
- `os_open_session(replace=...)` on a session that ran on an odoo.sh build now
  says that a remote target is a human's to open. It used to fail with
  "container, database and odoo_bin are required", because the journal has no
  `odoo_bin` for a remote target — an agent read that as a client bug and went
  hunting for a way around the rule instead of asking for a handover. The
  journal's `session_open` record gained `target_kind` and `host` so the
  refusal can be specific, and a journal written before that field still
  refuses, on the missing `odoo_bin`.
- The `commit`, `ownership` and `remote` help topics say all of the above, so
  the mechanism and the explanation cannot drift apart.

### The instruction budget

- A host delivers only the **first 2048 characters** of an MCP server's
  instructions. Measured, not assumed: two different hosts cut this server's
  text mid-sentence at exactly that offset and marked it truncated, and no
  resource or tool call reached the remainder. At 16 kB of instructions, 88%
  never arrived — and the daemon has no way to see that from its side.
- The cost was already paid twice. One agent wrote its own `logging` handler
  to capture a log `os_exec` returns, because the section explaining it sat at
  offset 9 000; another ran a queue job through `Job.load` rather than the
  documented `queue_job__no_delay` at offset 13 000. Both had read everything
  they were given.
- `INSTRUCTIONS` is now 1.8 kB and carries only rules an agent would break
  without knowing they exist: one command per session, rollback as the
  default state of the world, how commit is granted and where it is refused
  outright, whose session it is, and what is not theirs to touch. It also
  names every help topic, so what is missing stays discoverable even if the
  text is cut again by a stricter host.
- `os_help(topic)` returns the rest — thirteen topics (`sessions`,
  `ownership`, `commit`, `remote`, `limits`, `orm`, `code`, `log`, `records`,
  `modules`, `jobs`, `tests`, `watching`), read-only, opening no session. No
  guidance was deleted in the move, and a test pins that: every phrase that
  used to be in the instructions still has a home.
- Per-tool guidance stays in that tool's own description, which hosts deliver
  whole — verified up to the ~1 kB of `os_run_test`'s.

## [1.3.0] — 2026-09-05

Odoo 15 through 19, not 19 alone. Nothing above the bootstrap changed to
allow it: the frame protocol, the session state machine, the transport and
the API are untouched, because the facts the bootstrap rests on are
line-for-line the same in all five versions — the non-tty branch of Odoo's
own `console()`, the names `env` and `self`, the rollback around it, `SIGINT`,
and a cursor that commits on a clean exit. Every version was verified on a
real container, not by reading sources.

### Versions

- `SUPPORTED_MAJORS` (15, 16, 17, 18, 19) replaces the single supported
  major, and a refusal now names what would work: `Odoo 14.0 found;
  supported: 15, 16, 17, 18, 19`. It is still one gate, still at connect
  time, so an unsupported instance is refused before a command is ever run.
- Verified live on 15, 16, 17, 18 and 19: probe, session, `exec`, a namespace
  that survives between commands, structured errors, commit and rollback,
  interrupt, close, `list_tests` and a real `run_test`. On 15 the commit was
  read back from a second session and the rolled-back record was gone, since
  that path is the one that had to be rewritten.
- What moved between versions is **feature-detected in the bootstrap**, never
  keyed to a version number: the container is the only authority on what its
  Odoo has, and a number parsed from `odoo.release` would be a second,
  weaker one.
  - `env.flush_all()` and `env.invalidate_all(flush=False)` arrived in 16.
    Before that the same two things are `env['base'].flush()` and
    `env.clear()` — `Environment.clear` invalidates the cache and drops both
    `tocompute` and `towrite`, so it discards pending writes rather than
    writing them out, which is exactly what a rollback needs. The default
    `flush=True` would have written out what the rollback then threw away.
  - `odoo/tests/shell.py` arrived in 17 and is byte-for-byte the same file in
    17, 18 and 19, so those three call `run_tests` and nothing here
    reimplements it. 15 and 16 have no such file, but every primitive it is
    built from is older and unchanged — the tag DSL, `loader.make_suite`,
    `loader.run_suite`, the result object, `Registry._lock` — so
    `_os_run_tests_fallback` is that file's body against them, used only when
    `odoo.tests.shell` will not import. Its `workers != 0` refusal is kept and
    still reaches the caller as `TestRunnerRefused`.
  - The fallback drops the `odoo.cli.COMMAND != 'shell'` guard: `odoo.cli` has
    no `COMMAND` attribute before 17 (checked on a live 16 and 15), and the
    shell is where we already are.
  - `OdooTestResult` lives in `odoo/tests/result.py` from 16 and in
    `odoo/tests/runner.py` in 15, where its counters are unittest's lists
    rather than the `*_count` integers. `_os_test_counts` prefers the
    integers and falls back to `len()`.
  - `run_suite` gained `global_report` in 16. `_os_run_suite` reads
    `run_suite.__code__.co_varnames` rather than trying the keyword and
    catching `TypeError` — a `TypeError` raised inside a test is
    indistinguishable from a signature mismatch, and must not be retried as
    one.
- `odoo/tests/tag_selector.py` does differ (19 split a file-path variant out
  of the module part of the grammar), but the spec this project builds,
  `*/module:Class.method`, parses the same in all five. The rest of
  `cli/shell.py`'s diff across versions is the interactive branch — ipython,
  ptpython, `--shell-file` — which is dead code here, since stdin is never a
  tty.
- The bootstrap harness now installs a fake Odoo in three shapes (15, 16, 17)
  so all three runner paths and both transaction boundaries are covered
  without a container. `tests/test_e2e.py` no longer pins 19, so
  `PT_E2E_CONTAINER` can point it at any supported major.

### The Odoo log

- `exec` collects the log lines Odoo wrote while the command ran and returns
  them as `stderr`, the way `run_test` already did, plus `stderr_truncated`.
  The lines were always collected and always journalled; not handing them
  back cost a real agent seven lines of its own `logging` handler to scrape
  the log out of the process, because nothing in the response suggested a log
  existed. Clipped from the end, since the last line is the one that says
  what happened. The drain wait is paid only when lines actually arrived:
  `exec` is the hot path and a trivial command finishes in under a
  millisecond.
- For an agent the log is opt-in: `os_exec` always reports `stderr_lines`, a
  count, and `stderr=True` adds the lines. Both halves of that matter — a log
  in every response is context spent on output nobody asked for, and a
  response that says nothing about a log is what made an agent write its own
  handler. The count is the part that cannot be guessed. `os_run_test` still
  sends its log unasked, because there the log is the answer, and the journal
  holds everything either way, so a log wanted after the fact is read instead
  of re-produced — re-running a command that wrote to the database writes
  again.
- The Markdown transcript no longer drops `kind: "stderr"` records. It
  renders them as an `Odoo log` block after the command they belong to,
  consecutive lines in one fence. Markdown is the default export, so dropping
  them made a transcript claim the session had logged nothing — and took with
  it every traceback Odoo logs rather than raises, which is not an exception
  result and had no other rendering.
- `os_exec`'s tool description and the server instructions now say the log
  comes back with the command and that `os_journal(fmt="json")` has the whole
  of it. An agent told neither builds a logging handler instead.

### Agent access

- Instructions: read a record whole with `record.read()[0]` rather than
  naming the fields you expect — it returns every readable field, computed
  ones included, so a field the agent did not think to name is there anyway.
  The return is always a list, one dict per record. Relations are not
  followed: a many2one comes back as `(id, display_name)`, an x2m as a list
  of ids, so a level deeper is a second read. A wide model read whole is
  expensive, so it is one record to learn the shape, then `search_read` with
  the fields that mattered.
- Instructions: how to pick up a module's changed data, views or schema —
  `button_immediate_upgrade()`, and `button_immediate_install()` for a module
  that is not installed yet (dependencies come with it; an empty recordset
  means `update_list()` has not run since the module appeared on disk).
  Either commits on its own and so is a write the commit gate does not
  cover: the human is asked first, and never on a
  session running on someone else's instance. It is also how the module's
  migration scripts run, which is usually the point, and a script runs only
  if its version is above what `latest_version` records and no higher than
  the manifest's. Edited Python is not picked up at all — the process
  imported those files at startup — so that needs a new session.

## [1.2.0] — 2026-09-04

An odoo.sh build can be a target, not just a local container. The mechanism
did not change to allow it — the frame protocol, the session state machine
and the bootstrap are untouched — because the design never leaned on
anything `docker exec` and `ssh` do differently. Verified against a live
staging build, including an interrupt mid-command and a test run.

### Targets

- `Target` carries a kind. One identity slot, read differently: a container
  name locally, a build id on odoo.sh. `send_signal` takes the target rather
  than a container string — that argument was knowledge of containers
  leaking into the session layer.
- `POST /api/probe/odoosh` asks a build about itself: `$PGDATABASE`,
  `$ODOO_VERSION`, `$ODOO_STAGE`, and `odoo-bin` already on `PATH`. No path
  guessing, no config candidates, no database list — a build has exactly one
  database and the instance user cannot read `pg_database` anyway. A build is
  entered, not discovered; there is no `docker ps` for odoo.sh.
- `POST /api/sessions` takes `kind: "odoosh"` with `build`/`host`. The
  registry probes before it spawns, so an unsupported version is refused
  then rather than on the first command, and the stage comes from the
  instance rather than the request — a caller that could name its own stage
  would make the guard below decorative.
- We pass odoo.sh no arguments at all. Its `odoo-bin` wrapper appends
  `--database`, `--config`, `--workers=0` and `--no-http` after the
  caller's, so a `-d` of ours would be shadowed and would imply a choice the
  build does not have. `--workers=0` being forced there happens to satisfy
  `run_tests()`'s own precondition.
- SSH connections are multiplexed (`ControlMaster`/`ControlPersist`).
  Interrupt and kill each open a second connection: measured against a real
  build, a fresh handshake costs ~1.1s against ~0.13s — and 1.1s is the
  latency this tool exists to remove.

### Connect

- Two modes on the Connect screen: containers, discovered as before, and
  odoo.sh builds, which are entered and then kept. A probed build stays as a
  card so a 40-character hostname is typed once, not once per session, and
  carries the stage the instance reported.
- **Open session** on a build shows what a container start shows: the key
  goes inert with a spinning arrow, the note says an SSH connection and a
  registry load are in progress, and a log well streams the build's own
  stderr. SSH plus a registry load is the longest wait on the screen, and it
  used to be silent. A failed open leaves the well up with the error.
- **Probe** carries a neutral outline at rest. It is the key that makes a
  card exist and the only control on an empty screen, and `.primary` drops
  the border, so it was visible only under the cursor.
- A key that is working keeps its label: **Start**, **Open session** and the
  session keyboard's **New** blurred their word under the spinner instead of
  hiding it. An empty key reads as broken rather than busy. The ink moves
  into a narrow `text-shadow` — a `filter` on the button would have blurred
  the spinner along with it, and a wide radius smeared the word over the
  key's own outline. A busy **Start** also borrows a border, since `.primary`
  has none and the key stopped looking like a key.
- **Close session** on a card closes every session on that target, and the
  card counts them (`connected · 2 sessions`, **Close 2 sessions**). One
  click per session was the card behaving as if it stood for a session; it
  stands for a target, and a target legitimately holds several. The
  discard question is asked once for the batch.
- A build's **×** is disabled while that build has a session open, and says
  to close it first. The card is the only record of the hostname on this
  machine, so forgetting it left a live session with nothing pointing at it.
- Refresh is a **↻** beside the list it refreshes, not a button across the
  header — across the header it read as a peer of the mode control. It turns
  until the last probe answers, since the probes are the slow part.
- A refresh updates the container cards in place instead of rebuilding them.
  Ten full rebuilds per refresh, a blanked list and discarded probe results
  made the text jump; cards are now kept by name, keep the facts their last
  probe reported, dim them while they are re-checked, and nothing is written
  that has not changed.

### Writing to someone else's instance

- On a remote target commit is off until granted, for a human owner too.
  Locally owning the session is enough because the human confirms in the UI;
  owning a session is not the same as being entitled to write to someone's
  instance.
- On `production` commit is refused outright, as `commit_forbidden` rather
  than `commit_not_allowed`, and the attempt to *grant* the right is refused
  too — a guard that can be granted around is not a guard. The two codes
  must differ: the first means ask and then watch for the grant, the second
  means nothing will ever grant it, and an agent treating them alike would
  poll forever. `exec` and `rollback` are untouched.
- An agent cannot open a remote target at all, and this is enforced by
  omission rather than by a check: no MCP tool takes a host or a build. It
  reaches one only through a handover a human performed.
- `os_run_test(session_id=...)` runs in a session that was handed over,
  which on odoo.sh is the only way — otherwise the tests feature was
  unreachable exactly where staging lives. Such a session is not closed by
  the agent or by itself, and `discarded_pending` stops being theoretical:
  a lent session may hold the human's work, and Odoo's own runner rolls back
  a mid-transaction cursor before testing.

## [1.1.0] — 2026-08-28

The MCP server now spells out the working habits that were left as guesswork:
how to run delayed jobs in the shell, when to close a session, and how to
notice Grant commit without being told in chat. An agent can list one addon's
tests and run a class or method by name in its own session. The Sessions
screen tells a test run apart from ordinary `busy`, and a session clock sits
on the header.

### Agent access

- Instructions: put `queue_job__no_delay=True` on the environment context so
  `with_delay()` (and nested delays) run inline instead of enqueueing. This
  session still does not run the job queue for you.
- Instructions: a session may stay open across many steps; call
  `os_close_session` when the work is finished and you do not plan to
  continue.
- Instructions: after `commit_not_allowed`, explain the write, then poll
  `os_session` until `allow_commit` is true. Do not wait for a chat
  confirmation, and do not spin on `os_commit`. Once granted, later commits
  in that session need no further check-in.
- `os_session` — GET of the current session, including `allow_commit`. That
  is how an agent sees a grant flipped in the UI.
- `os_run_test` — runs one Odoo test method or a whole test class
  (`module.TestClass` / `module.TestClass.test_method`) through Odoo's own
  shell-native test runner (`odoo.tests.shell.run_tests`), not a
  reimplementation. Always opens its own brand-new session first, so it never
  has a pending transaction to lose; that session then closes itself (below).
  stdout and the Odoo log lines produced during the run come back separated.
  `tests_run: 0` is reported explicitly — Odoo's own result object reads as
  "successful" even when a name matched nothing. Calling it a second time in
  the same session after `os_exec` left work pending discards that work
  (Odoo's own test runner rolls back a mid-transaction cursor before testing);
  the response's `discarded_pending` field says so. The bootstrap picks a
  free port for the test HTTP daemon `odoo.tests.shell.run_tests()` spawns
  the first time it runs in a session, and sets it on the server object
  itself (not just `config['http_port']`, which is already too late to
  matter — the object's own `port` attribute was fixed at shell startup) —
  otherwise it collides with the container's own Odoo on the configured
  port and takes the session down with it.
- A `run_test` journal entry is now recognized by `os_history` and
  `os_journal` (previously only `exec` was) — its outcome (`tests_run`,
  `failures`, `errors`, `skipped`, `success`, stdout/stderr, duration) is
  shaped the same way `os_run_test` answers with, so a result is still
  recoverable after a client-side transport timeout on a long-running test
  class, the same way an abandoned `exec` already was.
- A client-side read timeout is now reported as `request_timed_out`, not
  `daemon_unreachable` — the daemon is still working, not down. The
  instructions tell the agent to wait on `os_test_result` rather than retry
  the call, which would otherwise start a second, duplicate run on top of
  the one already in flight.
- Instructions: how to find the code being debugged. A model's `__mro__`
  names every module extending it in resolution order, and
  `inspect.getsource` on the winning method answers what the filesystem
  cannot — on disk every override sits side by side with nothing to say
  which one is in effect. Non-Python files (views, data, manifests) go
  through `odoo.tools.file_open`, which stays inside the addons paths and
  refuses one that escapes them. Both were already possible from the shell;
  nothing said so, so nothing used them.
- `os_list_tests` — list test classes and methods in one addon (`module`,
  optional `container`), already shaped as `os_run_test` specs. Disk
  catalogue via `GET /api/containers/{container}/tests?module=`; read-only,
  opens no session. It lists what Odoo's own loader would run: any class
  carrying a `test_*` method (not only ones named `Test*`), and only in the
  modules `tests/__init__.py` actually imports, so a spec it hands out is
  never one that comes back `tests_run: 0` for having never been loaded.
- `run_test` collects the Odoo log lines of its own run as they arrive rather
  than slicing the session's rolling stderr tail. That tail holds 2000 lines;
  a real test class logs many times more, so the window used to come back
  empty on exactly the runs worth reading. Output past `RUN_STDERR_LIMIT`
  keeps the tail and sets `stderr_truncated`.
- The rebuilt command feed numbers `exec` and `run_test` on one counter.
  Two separate counters could hand the same number to two commands, and
  disagreed with the Markdown transcript for the same journal.
- `os_run_test` waits out the daemon's own registry-load ceiling when opening
  its session, instead of giving up first and stranding a session whose write
  key it never received. If the open does time out, the refusal carries the
  `client_token` to find that session by.
- A session opened by `os_run_test` closes itself once the run has settled
  and been journalled — nothing to remember, no container process or test
  HTTP daemon left holding on. Never set for a human's session. A run that
  blew its ceiling is not settled yet, so the close waits for the late
  result rather than killing a run still in progress; a process that died
  is reaped the same way.
- `os_test_result(session_id)` — waits for a run started by `os_run_test`
  and answers with its outcome, or `running` (call again), or `lost` if the
  run died with its process. Read-only and needs no write key, so it works
  after the session has closed itself and after this server restarts.
- `os_run_test` stops waiting at `MCP_CALL_BUDGET` (40s, override with
  `ODOO_SHELLER_MCP_BUDGET`) and answers `status: "running"` with the
  session id instead of being killed by the host mid-call. The daemon still
  gets the full `timeout` the caller asked for — only this server's own
  waiting is capped, so the run itself is never cut short. The budget covers
  the whole call, opening the session included: spending it on the run leg
  alone, on top of a registry load that already took seconds, overshot the
  host's own limit and the call died before it could hand back the session
  id. No leg may add a margin on top of the cap either — a margin over a cap
  defeats the cap.
- `os_run_test` clips `stderr` from the end rather than the beginning. The
  line worth reading is the last one (`Tests passed: …`, or the failure that
  ended the run); clipping from the front returned the test framework's boot
  chatter and dropped the answer. It also forwards the daemon's own
  `stderr_truncated`, which reports dropped *lines* and is a different loss
  from this server's character clip.
- `os_list_sessions` drops keys for sessions the daemon no longer has, so
  `yours` stops listing ids of sessions that already closed themselves.
- The same stderr tail rule and `stderr_truncated` flag now apply when a run
  is read back through `os_test_result` or `os_history`, not only when
  `os_run_test` answers directly — both go through the journal, where the
  clip was still taking the head.
- `run_test` rejects a timeout of zero or less (and anything over an hour):
  it would send the frame and abandon it in the same breath, leaving the
  session busy for the whole real length of the run.

### Web UI

- Session header meta line: local start time and a live age in seconds
  (`14:42:07 (18s)`). The tick updates that span only. A session this tab
  opened is stamped immediately; any other uses history `opened_at`.
- Cell feed hides its scrollbar (same pattern as the journal list). The
  CELLS heading stays put; cards size to their content so unfold still
  works when the feed is long.
- While a test is running, the session badge reads `testing` in Journals-warning
  rose. The session tab keeps its usual cyan/amber color and grows a blinking
  rose lamp instead (no animation when the OS asks for reduced motion). A short
  run still holds both for one pulse so they are readable. Ordinary `exec`
  stays cyan `busy`. The daemon names the in-flight work as `activity` on
  `describe()` and on WebSocket `state` events (`run_test` / `exec` / `null`).
  An empty cell feed during a test run says to open Logs rather than
  offering `⌘+Enter`. A `run_test` journal entry is not drawn as a
  transaction marker.
- Watching a session, the first open of the log keeps the editor's gap under
  the session keyboard (same restack as log focus). Previously it sat flush
  against the keys until expand/collapse was clicked.

## [1.0.0] — 2026-08-21

A persistent Odoo 19 REPL behind an HTTP/WebSocket API: the daemon keeps
`odoo-bin shell` alive inside a local Docker container, so the registry loads
once instead of once per command. A web UI drives it; an MCP server gives an
agent the same API. Rollback is the default everywhere and commit is always an
explicit, confirmed act.

### Daemon and protocol

- Persistent session over `docker exec`: the bootstrap runs inside the
  container's own Python and takes over the point where `odoo-bin shell` would
  start an interactive console. Nothing is installed in the container and
  nothing is copied into it — the bootstrap arrives as a heredoc.
- `exec 3<&0` keeps the command pipe alive on fd 3 after Odoo replaces its own
  stdin with that heredoc.
- Frames on stdout, Odoo's logs on stderr — split by stream, not by markers.
  The bootstrap takes a private dup of fd 1 and points fd 1 at stderr, so a
  stray `print` from a background thread cannot corrupt the stream.
- Payload ceilings (`MAX_STDOUT`, `MAX_RESULT`) shared between `protocol.py` and
  the bootstrap, with the subprocess reader raised to match: a clipped frame is
  still far larger than asyncio's default 64 KiB line limit, and a reader that
  dies there would leave the session busy forever.
- Live target discovery: `docker ps` for containers, then a one-shot probe for
  odoo-bin, Odoo and Python versions, config path and database list. Odoo 19 is
  verified and supported; anything else is refused at connect time.
- Binds `127.0.0.1` only, with no authentication. An admin key guards actions on
  sessions you do not own — an accident guard, not a security boundary.

### Sessions

- One command at a time per session. A second is refused, never queued.
- `ready` is reached on the bootstrap's `hello` frame, never on a timer.
- Explicit transaction control. Commit is `flush_all()`, `cr.commit()`,
  `invalidate_all(flush=False)`; rollback is `invalidate_all(flush=False)` then
  `cr.rollback()`. The `flush=False` matters: the default would write out
  exactly what the rollback is about to discard.
- `SIGINT` interrupt that surfaces as `KeyboardInterrupt` inside the command.
- A command that blows its ceiling keeps the session busy until it really ends —
  `SIGINT` is a request, not a guarantee — and its late result is journalled as
  `abandoned_result`. `close` is accepted even while busy.
- Process death is a normal outcome: EOF on stdout moves the session to `dead`
  and pending requests fail with the tail of stderr attached.
- Many sessions, keyed by id, each with its own target. Sessions cannot outlive
  the daemon.

### HTTP and WebSocket

- `POST /api/sessions` blocks until `hello`. Watchers hear `session_starting`
  as soon as the session is registered, then `session_opened` when it is ready.
  A start that never reaches `hello` emits `session_failed` with the reason, so
  a watcher does not keep a session that never became ready and never went away.
- `POST /api/sessions` accepts an opaque `client_token`, echoed back in
  `describe()`. Container and database do not identify a session — a container
  may hold several, and an agent can be opening the same target — so the token
  is how a client recognises its own `session_starting`.

### Ownership and agent access

- Every session has an owner (`human` or `agent`) and a write key returned once,
  at open or at handover, and required by `exec`, `commit` and `rollback`.
- Handover moves the right to type without disturbing the process, the namespace
  or the open transaction. The human stays admin: they watch any session,
  interrupt, close, kill, hand it around and grant commit.
- `allow_commit` gates an agent only; a human owner confirms each commit in the
  UI instead.
- `odoo_sheller/mcp.py` — the agent's client over stdio, with no logic of its own.
  Refusals carry what the agent needs to act on them: `session_busy`,
  `session_gone`, `not_owner`, `commit_not_allowed`.
- The server's instructions cover idiomatic ORM usage: `mapped()` instead of a
  hand-rolled loop — including the non-obvious part, that a dotted path like
  `partner_id.bank_ids` returns the de-duplicated union as a recordset, not a
  list of lists — `filtered()` / `sorted()`, pushing a filter into `search()`
  instead of fetching broadly and filtering in Python, `search_count()` over
  `len(search(...))` when only a count is needed, the set operators (`|` `&`
  `-` `in` `<=` `<` `>=` `>`) recordsets support directly, and `ensure_one()`
  for a helper meant to run against a single record.
- The instructions are explicit that `allow_commit` gates `os_commit` only —
  `os_exec` is never gated by it — and that once the human grants commit, the
  agent calls `os_commit` directly for every later commit in that session; the
  ask-first ritual is for the first refusal, not a standing requirement to
  check in before every commit.

### Web UI

- Three screens — Connect, Sessions, Journals — served from `odoo_sheller/web/`
  with no build step. CodeMirror is vendored; the page makes no external
  requests. The last screen and the last used target are remembered in
  `localStorage`.
- Session pane: editor, cell feed, and an Odoo stderr split with a level
  filter, a tail that follows only while you are at the bottom, and a focus
  mode that hides the editor and the cell feed so the log fills the whole
  pane — its top border lines up exactly where the editor's did, rather than
  sitting right under the session keyboard.
- Cells fold to their header — the whole header is the target, not just the dot.
  Agent-authored cells arrive folded; your own arrive open.
- A two-row session keyboard where keys stay in place: a control that does not
  apply is disabled, never hidden.
- Connect's **Start** streams the container's stderr into a 12-line log well
  while Odoo loads its registry, paced into the well rather than dumped in one
  paint. A failed start keeps the well so the last lines stay readable next to
  the error. The well follows the tail only while you are at the bottom; scroll
  back to read a traceback and a re-render leaves you there. **Refresh** while a
  session is opening no longer takes the container list down with it: the picker
  waits for the re-probe, the log well does not.
- Journals screen: rows grouped by container and database, a sticky column
  header, a transcript preview, and per-row and per-group deletion.
- Assets are served `no-store`, so there are no hand-maintained cache busters.

### Journals

- Append-only JSONL per session under `~/.odoo-sheller/journals/`: open with target
  and versions, every command with its code, every result in full, transaction
  boundaries, interrupts, process death, and Odoo's stderr interleaved by time.
  The file on disk is never rewritten.
- Outlives both the session and the daemon. Exportable as raw JSONL or as a
  Markdown transcript, both carrying session metadata.
- Truncation is a display concern only: the bootstrap caps payloads to protect
  the pipe, the journal stores whatever arrived in full, and the API shortens it
  for the UI.
- `/api/sessions/{id}/history` answers for a closed session too, with
  `session.state: "gone"` and a `session.gone` object saying how to recover.
- Deleting a journal takes the admin key and matches the id exactly — the one
  irreversible file operation in the API.
- **Journals are unmasked.** They can contain credentials read from the
  database. They stay local, stay out of git, and must be reviewed before being
  shared.

### Documentation

- A `docs/` folder, tracked in git: `architecture.md` (protocol, session state
  machine, journal format), `ui-guide.md` (every screen and control),
  `agent-guide.md` (MCP tool list, Claude Desktop wiring), `security.md` (the
  actual threat model, and why journals aren't masked), and an FAQ in English
  and Russian. `docs/README.md` indexes all of it.
- Replaces the working notes this project used while it was being built —
  design specs and task-by-task plans written for an AI pair-programming
  session, not for someone reading the repository afterward. Those stay out
  of git; nothing they said that still matters was left out of the new pages.
- README and CLAUDE.md point at the new pages instead of the old ones.

### Cleanup

- The rebrand from py-tunnel missed three environment variables: `PT_TUNNEL_URL`
  is now `ODOO_SHELLER_URL`, and the bootstrap's `PT_CMD_FD` (a test-only knob,
  never set in a real run) is now `OS_CMD_FD`. `PT_AGENT_LABEL` is gone
  entirely — it was never actually overridden, so the agent's session label is
  now just the constant `"mcp-agent"`.

### Known limitations

- Odoo 19 and local Docker only. Remote hosts over SSH and odoo.sh are out.
- `/docs` is the one page that needs network: the swagger-ui bundle comes from a
  CDN. `/openapi.json` is served locally.
- Journal masking is not implemented, and there are no production-database
  guards — local Docker only, by definition.
- Deferred: outgoing HTTP tracing, `changed` record diffing, synchronous
  `with_delay`, and live streaming of output while a command runs.

[1.8.3]: https://github.com/romi477/odoo_sheller/compare/v1.8.2...v1.8.3
[1.8.2]: https://github.com/romi477/odoo_sheller/compare/v1.8.1...v1.8.2
[1.8.1]: https://github.com/romi477/odoo_sheller/compare/v1.8.0...v1.8.1
[1.8.0]: https://github.com/romi477/odoo_sheller/compare/v1.7.0...v1.8.0
[1.7.0]: https://github.com/romi477/odoo_sheller/compare/v1.6.4...v1.7.0
[1.6.4]: https://github.com/romi477/odoo_sheller/compare/v1.6.3...v1.6.4
[1.6.3]: https://github.com/romi477/odoo_sheller/compare/v1.6.2...v1.6.3
[1.6.2]: https://github.com/romi477/odoo_sheller/compare/v1.6.1...v1.6.2
[1.6.1]: https://github.com/romi477/odoo_sheller/compare/v1.6.0...v1.6.1
[1.6.0]: https://github.com/romi477/odoo_sheller/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/romi477/odoo_sheller/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/romi477/odoo_sheller/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/romi477/odoo_sheller/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/romi477/odoo_sheller/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/romi477/odoo_sheller/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/romi477/odoo_sheller/releases/tag/v1.0.0
