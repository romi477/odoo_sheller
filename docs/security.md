# Security model

Read this before pointing odoo-sheller at anything you'd mind losing or
exposing. The short version is in the README; this is the reasoning behind
it.

## The core fact

odoo-sheller executes arbitrary Python as Odoo's superuser (`SUPERUSER_ID`)
against a real database. It is a debugging tool for a developer who already
has that level of access to their own local containers — it does not add a
new privilege, it just makes using an existing one faster. Every other
decision here follows from taking that seriously.

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

**There is no production-database guard.** The tool doesn't try to detect or
block a connection to a database that looks like production, because the
entire design assumes local Docker only — and a local database, by
definition, is not the one serving real traffic. If that assumption stops
holding for you, the guard needs to exist *before* you point this at
anything you can't afford to lose, not after.

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

Until it is, the practical rule is:

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
| Anyone holding the admin key | Act on any session or journal, including ones they don't own | Bypass Commit's requirement for a human confirmation on a human-owned session |

If your threat model includes "another user on this machine, or malicious
code already running as you," none of the above is a defense — at that
point the database itself is already compromised, with or without
odoo-sheller in the picture.
