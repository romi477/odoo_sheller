# FAQ

Plain-language questions and answers about how odoo-sheller works. A Russian
version lives at [faq-ru.md](faq-ru.md); for the technical version of most of
this, see [architecture.md](architecture.md).

## What is this, in one sentence

It keeps an Odoo shell open inside a Docker container and lets you type code
into it from a browser — write an ORM command, press `⌘+Enter`, see the
result, and the shell stays open for the next command.

## Why not just `docker exec ... odoo-bin shell`?

You can, and for a single command there's no reason not to. The problem
shows up over a debugging session with dozens of attempts: each `docker exec
... odoo-bin shell < script.py` pays about 1.4 seconds of registry loading,
plus the boilerplate around it — writing the script to a temp file,
remembering the right flags, adding markers to cut your output out of the
log, filtering stderr by hand. odoo-sheller pays that startup cost exactly
once per session; every command after that runs over an already-open
channel.

## Who is this for?

Right now: you, through the browser. Alongside that: an AI agent, through
the [same API](agent-guide.md) — there is no separate, smaller interface for
an agent. Whatever the browser can do, an agent's tools can do too, subject
to the same ownership and commit rules.

## How it's built

**What are the moving parts?**

Three of them:

1. **The daemon** — a Python process on your machine, listening on
   `localhost:8765`.
2. **The web page** — runs in your browser, talks HTTP/WebSocket to the
   daemon.
3. **The bootstrap** — a small loop that runs *inside* the container and
   actually executes your code.

The daemon is the only piece that knows about pipes and containers. The
browser only ever speaks HTTP.

**Does it reimplement Odoo's shell?**

No, deliberately not. `odoo-bin shell` already does all the real work —
parsing the config, building the registry, assembling `env`, rolling back on
exit. odoo-sheller only replaces the one moment where that command would
hand off to an interactive console. The less of Odoo's own behavior gets
re-implemented, the less there is to break when Odoo changes.

**How does code get in, and results come back?**

Over a pipe held open by `docker exec`. A line of JSON with your code goes
in; a line of JSON with the result comes back — what printed, what was
returned, any error, how long it took.

Odoo's own logs travel on a separate stream (stderr) and never get mixed
into the results, so there's nothing to filter or cut out by hand.

## Containers and sessions

**Does this create containers?**

No — never. You start your containers exactly as you did before;
odoo-sheller only ever connects to ones that are already running.

**Then what does it create?**

Two kinds of short-lived process, both inside your existing container:

- **A probe** — lives about a second. Runs automatically for every container
  `docker ps` reports, when you open the Connect screen. Reports whether
  `odoo-bin` exists in there at all, which Odoo version, which Python,
  where the config file is, which database the config points at, and which
  databases actually exist in Postgres. Then it exits.
- **A session** — lives until you close it. Starts on one event: you picked
  a database and pressed **Start**. The daemon runs `odoo-bin shell` inside
  the container, it loads the registry, says hello, and waits for commands.
  While the registry loads, the container's stderr streams into the Connect
  card so "loading" isn't a silent wait.

**Where does the default database name come from?**

From `db_name` in the container's `odoo.conf`, if it's readable. In a dev
container that's almost always the one you actually want. Every other
database Postgres reports is also listed, and if the list couldn't be read
at all, you can type a name by hand.

**When does a session die?**

Four ways: you press Close, you press Kill, the daemon itself exits (its
pipe breaks, the in-container process follows), or the container restarts.
There's no fifth way — a session never dies on its own from inactivity or a
timer.

**Can I have several sessions at once?**

Yes, shown as tabs. Each has its own process, its own namespace, its own
**transaction** — against the same container, or different ones.

Worth knowing: two sessions against the same database don't see each
other's uncommitted work until one commits and the other starts a fresh
transaction. That's not something odoo-sheller adds or could remove — it's
plain Postgres transaction isolation.

**Does closing a session hurt the container?**

No. Only odoo-sheller's own process ends. The Odoo server and the container
itself keep running.

## Data and transactions

**Are my changes saved automatically?**

No. By default, **nothing is saved**. This is the one rule everything else
is built around.

Everything your code does lives inside an open transaction. Close the
session, and it's all rolled back — that isn't odoo-sheller's own invention,
it's what `odoo-bin shell` already does on exit; the design just makes sure
nothing breaks that.

**So how do I actually save something?**

The **Commit** button. It's the only control in the whole interface that
persists anything, which is why it asks you to confirm, naming the database,
every single time.

**If it rolls back by default anyway, what's Rollback for?**

Throwing away what's accumulated so far *without* closing the session.
Create some test records, look at what happened, roll back — the
environment is clean again and your variables are still there.

**I changed something through Odoo's own web interface — why doesn't my
session see it?**

Because your session is sitting inside its own transaction and caching
whatever it already read. Press **Commit** or **Rollback** — either one ends
the transaction, clears the cache, and the next read picks up current data.

This is also the easiest way to get a silent lie: calling
`env.cr.commit()` by hand in the console ends the transaction but leaves the
cache stale, so records you look at afterward can show old values. The
Commit and Rollback buttons do both steps (commit or rollback, *then*
invalidate the cache) in the right order — use them instead of touching the
cursor directly.

**Does the "N commands since the last boundary" counter mean N changes?**

No — it counts *commands*, not writes. There's no reliable way for the tool
to know whether your code actually wrote anything. It only answers "have I
done anything at all since the last commit or rollback".

## Running commands

**Can two commands run at the same time?**

No. One session, one command at a time. A second one gets refused with
"busy".

There's no queue on purpose — a queue would misrepresent when a command
actually runs. Need real parallelism, open a second session.

**How do I tell a test run from ordinary busy?**

The session badge reads `testing` in rose instead of cyan `busy`, and that
session's tab grows a blinking rose lamp. A run that finishes in a blink
still holds both for about a second so they are readable. An ordinary
`exec` does not light the lamp.

**Do variables persist between commands?**

Yes, that's the entire point. Assign something in one command, use it in the
next. An ordinary REPL.

**Do I need `print()` to see a result?**

Not if the last line is an expression — its value shows up on its own, the
same way a plain Python interpreter echoes an expression typed at the
prompt.

**What if a command hangs?**

**Interrupt.** It sends a signal into the container, which raises
`KeyboardInterrupt` inside your code — the command stops, and the session
stays alive with everything it had. If a command runs past five minutes, the
daemon interrupts it for you and reports a timeout.

**Why don't I see output while a command is still running?**

In this version, output arrives all at once, when the command finishes.
While it runs you get a spinner and an elapsed-second counter. Streaming
partial output was left out on purpose — it complicates both the bootstrap
and the daemon, and most commands finish in a few seconds anyway.

**What happens with very large output?**

It's shortened for display, but **written to the journal in full**. A note
under the truncated text links to the complete version.

## The journal

**What's the journal?**

A file the daemon writes as it goes: when the session opened and to what,
every command with its code, every result in full, every commit and
rollback, every interrupt, the process dying, and Odoo's own logs — all
interleaved by time, so you can tell which log lines a given command
produced.

Lives at `~/.odoo-sheller/journals/`, one file per session.

**Does it survive closing the session?**

Yes — and closing the daemon too. The Journals screen lists every past
session, grouped by container and database.

**What's it actually for?**

Three things: checking what you did yesterday; recovering output that was
truncated on screen; exporting a transcript as Markdown — for a report, or
to hand to an agent.

**What about secrets in there?**

The journal is **not masked**. If your code read an API key or a password
out of the database, it's sitting in that file in plain text. See
[security.md](security.md) for exactly why doing this properly is harder
than it sounds, and what to do in the meantime — short version: journals
stay local, stay out of git, and get read before they get shared.

## Security

**How risky is this, really?**

Risky enough to say plainly: it runs arbitrary code with Odoo superuser
rights against a real database. The one architectural defense is that
nothing is saved by default — get something wrong, close the session, it's
gone.

**Is the daemon reachable over the network?**

No. It listens on `127.0.0.1` only, with no password. Whoever can reach the
port can run code. **This port must never be exposed to a network.**

**What about pointing it at a production database?**

There's no guard against that, on the assumption that this tool is for
local development only, and a local database is by definition not a
production one. If that assumption ever stops being true for you, that
guard needs to be built *before* the fact, not discovered missing after.

## Limits

**Which Odoo versions are supported?**

15 through 19. Anything older is refused by the probe immediately, with a
message naming what would work, rather than letting you hit a confusing
failure on the first real command.

The bootstrap depends on nothing that has changed across those five: the
non-tty branch of Odoo's own console, the names `env` and `self`, the
rollback around it, `SIGINT`, and a cursor that commits on a clean exit. All
five were checked by hand on real containers — session, commands, interrupt,
commit and rollback (a committed record read back from a second session), a
real test run.

Some things did move, and the bootstrap asks the container what it has rather
than reading a version number: the calls that flush and discard pending work
were renamed in 16, the shell's own test runner (`odoo/tests/shell.py`)
arrived in 17, and before that the test result object sat elsewhere and
counted in lists. For 15 and 16 the tests therefore run through the pieces
that runner is made of — same tag syntax, same suite loader — all older than
15. From the outside a test run looks identical on every version.

**Remote hosts, SSH, odoo.sh?**

An odoo.sh build, yes — a human opens it from the UI over SSH. A plain
self-hosted box, no: it would need sudo, path discovery and a database list,
which is a separate job.

**What's still missing?**

Tracing outgoing HTTP calls, showing "which records changed", running
`with_delay` jobs synchronously, and streaming output live while a command
runs. All of this is left room for in the protocol and the API — the UI just
doesn't pretend any of it exists yet.

**What do I need to install?**

On the host: Python and a virtual environment with the project's
dependencies, plus the `docker` CLI (which you already have if you're
running Odoo in containers). Inside the target container: **nothing at
all** — the bootstrap only needs `sh` and whatever Python the container
already ships with Odoo.
