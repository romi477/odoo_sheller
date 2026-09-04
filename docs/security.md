# Security model

Read this before pointing odoo-sheller at anything you'd mind losing or
exposing. The short version is in the README; this is the reasoning behind
it.

## The core fact

odoo-sheller executes arbitrary Python as Odoo's superuser (`SUPERUSER_ID`)
against a real database. It is a debugging tool for a developer who already
has that level of access — through `docker exec` into their own container, or
through SSH into an odoo.sh build they can already reach. It does not add a
new privilege, it just makes using an existing one faster. Every other
decision here follows from taking that seriously.

What changed when the second kind of target arrived is not the privilege but
*whose machine it is*. On a local container the database is yours and the
blast radius is your own dev data. On an odoo.sh build it may be a clone of
production, or production itself. The sections below say which guarantees
that changes and which it does not.

## What is protected, and how

**Nothing is written unless you say so, explicitly, every time.** This is
the one architectural safety net and it is load-bearing: closing a session,
killing it, or letting the daemon die all discard whatever the session's
open transaction was holding. There is no autosave, no "save on exit". The
only way anything reaches the database is the **Commit** button (or
`os_commit` for an agent, which additionally requires a human to have
granted the right first). See [architecture.md](architecture.md#committing-and-rolling-back)
for exactly what commit and rollback each do to the ORM cache and why the
order matters.

**Session keys are an accident guard, not an access-control boundary.** The
daemon and every client of it (browser, agent) run as the same local user.
A write key stops one browser tab from accidentally typing into a session
another tab or an agent is using — it does not stop anyone who can already
run code on your machine, because they could just as easily open their own
session. Don't read more security into the key system than that.

**The admin key exists for the same reason, one level up.** It's needed to
act on a session you never owned, or to delete a journal file — actions
where "any local process can do this anyway" isn't quite true, because they
affect someone else's in-progress work. It is never served by any API
endpoint: the daemon prints it once at startup and keeps it in
`~/.odoo-sheller/admin.key`, because an endpoint that returned it would hand
it to anything able to fetch a page from the same unauthenticated daemon.
Read it with `cat ~/.odoo-sheller/admin.key` and paste it into the UI once —
it's only asked for when the daemon actually refuses something, never
up front.

## What is deliberately *not* protected

**The daemon has no authentication and binds `127.0.0.1` only.** Anyone who
can reach the port can run code as your database's superuser. This is not a
gap to be filed as a bug — the daemon is not designed to be reachable from
anywhere but the machine it runs on, and it must never be exposed on a
network interface, tunnelled, or reverse-proxied. If a future version needs
to be reachable remotely, that needs a real authentication design first, not
a workaround.

**There is no guard on a local database.** The tool does not try to guess
whether a container's database looks like production: on your own machine, a
local database is not the one serving real traffic, and a guess would be
wrong in both directions.

**On odoo.sh there is a guard, because the instance says what it is.**
`$ODOO_STAGE` is readable over SSH before a session is opened, so the kind of
instance is a fact rather than a guess. Three things follow, and they are the
whole of the remote safety story:

- **Only a human opens a remote target, from the UI.** This is enforced by
  omission, not by a check: `os_open_session` takes a container, a database
  and an odoo_bin, and no host or build. An agent has no way to *name* an
  odoo.sh instance, staging or production, so it reaches one only through a
  handover a human performed deliberately. A guard with nothing to forget.
- **On a remote target, commit is off for everyone until granted.** Locally a
  human owner may commit at will, because they confirm each one in the UI and
  the flag exists to gate an agent. Owning the session is not the same as
  being entitled to write to someone's instance, so there the flag gates the
  human too.
- **On `production`, commit is refused outright.** Both the commit and the
  attempt to grant the right raise `commit_forbidden` — a guard that can be
  granted around is not a guard. Reading is untouched: `exec` and `rollback`
  work, because inspecting a production instance is the legitimate case and
  only writing is not.

The refusal codes differ on purpose. `commit_not_allowed` means ask the human
and then watch for the grant; `commit_forbidden` means nothing will ever
grant it, and an agent that treated them alike would poll forever.

**The daemon still never listens anywhere but `127.0.0.1`.** Reaching *out*
over SSH is not the same as being reachable: the connection is outbound, the
API surface is unchanged, and the rule above — never expose the port, never
tunnel *to* it — is exactly as absolute as it was.

**Journals are unmasked.** This is the one that's easy to get burned by, so
it gets its own section.

## Journals: what's in them, and why they aren't scrubbed

Every session's journal (`~/.odoo-sheller/journals/*.jsonl`) records the full
code you ran and the full output it produced — stdout, the returned value,
tracebacks — in plain text, forever, until you delete the file yourself. If
your code reads an API key or a password out of the database and prints it,
returns it, or it shows up in a traceback, it is now sitting in that file
exactly as read.

This isn't an oversight left for later — it's a genuinely harder problem
than it looks, and doing it badly is worse than not doing it. A naive
approach — mask any value under a dict key named `"password"` or `"key"` —
sounds reasonable and fails silently on real Odoo data: a webservice
integration once stored its key as `{"name": "key", "value": "sk_live_..."}`,
which a name-based filter walks right past, because the *value the field
means* isn't in a key called `key` — the field named `key` is a label, and
the secret is in the sibling field called `value`. Masking that correctly
means understanding the meaning of a field, not pattern-matching its name,
and that's a real piece of work, not a config flag. It isn't in this
version.

**A remote session's journal holds that instance's data, on your machine.**
This was weighed and accepted rather than overlooked. The reasoning: this is
a debugging tool pointed at our own staging builds, those instances and their
data are ours, and production access is not expected to be granted to it at
all — which is also why production refuses a commit rather than merely
warning. Paying for meaning-aware masking to protect our own staging data is
not worth it today.

Revisit that the moment this is pointed at an instance whose data is not
yours. The smaller of the remaining options is to drop payloads for remote
sessions — keep the code, the transaction boundaries and the test counters,
drop stdout, the returned value and the log. Test-result recovery survives
it, because what needs recovering is the counters and what is sensitive is
the log.

Until any of that, the practical rule is:

- Journals stay on your machine. They're excluded from version control by
  default (`~/.odoo-sheller/journals/` is outside any repo) — keep it that
  way.
- Before you export a transcript (`.jsonl` / `.md`) or copy one to share it —
  in a bug report, a chat message, anywhere — **read what you're about to
  send.** The UI warns you every time you export or delete, precisely
  because this is the one place a habit of clicking through warnings costs
  something real.
- Deleting a journal (the trash icon, or `DELETE /api/journals/{id}`) is
  permanent and needs the admin key for exactly this reason — it's the one
  irreversible file operation the API exposes.

## Threat model, summarized

| Actor | Can do | Cannot do |
|---|---|---|
| Anyone on `127.0.0.1` | Open sessions, run arbitrary code, read/write the database (after Commit) | Anything requiring network access to the daemon — there isn't any |
| A browser tab that isn't a session's owner | Watch that session's output live | Type into it, commit, or grant itself commit rights |
| An agent with a session of its own | Run code, rollback, read journals it can reach | Commit, without a human granting it first; act on a session it wasn't opened in or handed |
| An agent handed a remote session | Run code, run tests, rollback, read the instance | Open a remote target itself; commit until granted; commit at all on `production` |
| Anyone holding the admin key | Act on any session or journal, including ones they don't own | Bypass Commit's requirement for a human confirmation on a human-owned session |

If your threat model includes "another user on this machine, or malicious
code already running as you," none of the above is a defense — at that
point the database itself is already compromised, with or without
odoo-sheller in the picture.
