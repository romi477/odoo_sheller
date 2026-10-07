# Using the web UI

A walkthrough of the three screens — Connect, Sessions, Journals — and every
control in them. The UI is built for one developer working against their own
local Odoo containers, many times a day, in short bursts: speed of the loop
(type code, see result) matters more than discoverability, so a few controls
reward knowing they're there.

The UI is served at `/web` (`/` redirects there) and is a plain client of the
HTTP/WebSocket API described in the [README](../README.md#api) — an agent
using the [MCP server](agent-guide.md) can do everything a browser can, and
the two see each other's sessions live.

## Guiding rules

Worth knowing before the controls, because they explain *why* things behave
the way they do:

- **The session is the subject.** Its state is meant to be readable at a
  glance: which container, which database, alive or not, busy or idle.
- **Nothing is written to the database without an explicit act.** Rollback is
  the default everywhere; Commit is the only button that persists anything.
- **No hidden truncation.** When output is shortened for display, the UI says
  so and links to the full text.
- **Odoo's own logs are one click away, never in the way.**

## Connect

Three places, chosen with the radio group in the header — Local, Odoo.sh,
Server (SSH) — because they differ in kind rather than in parameters: local
containers are **discovered** (cards you did not ask for), an odoo.sh build and
a server are **entered**. The mode you used last comes back on a reload, the way
the screen does.

The cards of the two entered kinds are kept by the daemon, not by the page
(`~/.odoo-sheller/targets.json`): a desktop-app frame does not keep
`localStorage`. Writing, changing, deleting and probing one needs the admin key,
asked for the first time one of those is refused; the list needs none. Cards that
an earlier version kept in the browser move across once, when the odoo.sh list is
first opened.

On each of the two entered screens there are two regions, and nothing else. The
**form** is a dashed panel under its own title (*New server*, *Add a build*): it
is where a card is written, and it is drawn as a draft so that it does not read as
one more card. Changing a card puts it back in that panel, which turns amber and
says *Editing ‹name›*. Under it, headed by a rule, are the **saved cards** — a
title with a count (*Saved servers · 2*) and the cards themselves. A card under
the pointer is outlined in the brand cyan, on every screen.

Both forms fold. A form's title is a button with an arrow — down while the form
is open, right once it is folded — and a click on it folds the form to that title,
or opens it again. They are **folded until someone opens them**, since most visits
are to open a card already written; a form left open stays open on the next visit
(each remembers its own choice), and a page that cannot keep the choice stays
folded. A draft written in one survives being folded. Changing a card opens the
server form for as long as it takes, and when the change is saved or dropped the
form goes back to how it was left.

### Local

A list of running containers, one card each. The **↻** beside the title
re-runs `docker ps` and probes everything again; it turns until the last probe
has answered, not until `docker ps` came back, because the probes are what
take the time. It sits with the title because it acts on the list under it —
a button across the header would have read as a peer of the mode control.

A refresh does not redraw the screen. Cards are kept and updated in place,
each keeps the facts its last probe reported and dims them while they are
being re-checked, and nothing is written that has not changed. The list held
still through a refresh in the browser: same nodes, same heights, one DOM
insertion instead of a hundred. A card only appears or disappears when the
container did. Probing happens automatically
when the screen loads, and each card also has its own probe button for after a
restart or a config change.

- A container that already has a session shows a `connected · <database>`
  badge and offers **Close session** next to **Open session** — a *second*
  session on the same container, against another database, is entirely
  normal and allowed. Once there are several the badge counts them
  (`connected · 2 sessions`) and the key reads **Close 2 sessions**: a card
  here stands for a target, not for one session, so it closes all of them.
  Uncommitted work is discarded, and the question is asked once for the
  batch rather than once per session.
- **Open session** expands the card into a database picker, and becomes
  **Start** in the same place. The two are one control at two moments, so the
  second click of opening a session lands where the first one did rather than
  across the card. What the picker holds is the target and nothing else: which
  database, and a faint two-sheet copy glyph at its far edge that puts
  container, database and Odoo version on the clipboard as one line of JSON —
  what an agent has to be told before it can work. It turns into a green check
  for a moment, because a clipboard leaves no other mark; a word there would
  have read as a third key beside **Start** and **probe**, and that row holds
  no actions. The default database comes from `db_name` in the
  container's `odoo.conf`, which in a dev container is almost always the right
  answer. If the database list couldn't be read at all, a free-text field takes
  its place.
- A card stacks its name above its keys only when the width is really gone —
  below 520px, a phone. The page gives up its margins earlier, at 760px, but a
  narrow desktop column is not a phone: at 684px, the width the desktop app's
  **Compact Width** uses, the card's row needs 241 of the 626 it has.
- Probe failures are explained specifically — "no odoo-bin found in this
  container", "Odoo 12.0 found; supported: 15, 16, 17, 18, 19, 20 (13, 14: not fully tested)", "could not read database
  list — enter the name manually" — rather than as a generic error. A
  container that cannot host a session at all — no `python3` inside, or no
  `odoo-bin` — is folded under *N containers are not Odoo images* at the end of
  the list, greyed rather than red: a database container beside the Odoo it
  serves is the ordinary case, and there is nothing on that card to act on. The
  fold remembers whether it was opened. A container that fails its probe for
  any other reason keeps its card and its probe button; it
  just can't be opened until whatever's wrong is fixed.
- Each cell's header ends with two numbers, `written/came back`: lines of code
  over lines of output — stdout, the returned value and any traceback, the
  same three pieces **copy output** puts on the clipboard. A trailing `+`
  means the daemon clipped the output and the journal has the rest. Cells an
  agent wrote arrive folded, so this is the only place the size of a script
  is visible without opening it; the figures are tabular, so a column of
  folded cells can be compared at a glance.
- Opening a session takes a few seconds while Odoo loads its registry.
  **Start** goes inert with a spinning arrow over its own label, which stays
  legible as a blurred shape rather than vanishing — a key that empties reads
  as broken, not as busy. A log well at the bottom of
  the card streams the container's stderr as it arrives — so "loading" isn't
  a black box. A failed start leaves that well up, with the last lines
  readable right next to the error. Scrolling back in the well to read a
  traceback holds your position even while more lines arrive; a **Refresh**
  started mid-open leaves the well alone too.
- The card tracks the specific session it asked for, not just "a session on
  this target" — a container can hold several sessions, and an agent might
  be opening the same database at the same moment. The two don't get crossed.
- The last container and database you used are remembered and preselected
  next time.

### odoo.sh

Two fields — build id and hostname — then **Probe**, which only reads what the
instance says about itself: its database, its Odoo version, and whether it is
`staging` or `production`. Nothing is opened by probing.

A probed build becomes a card and stays in the list, so a 40-character
hostname is typed once rather than once per session. Each card carries what
its last probe said, and `production` is the one badge on this screen that
fills solid rather than tinting — it is the word worth interrupting for. The
**×** removes a card and changes nothing on the instance; it is disabled
while the build has a session open, and says so, because this card is the
only record of that hostname on this machine and the session would be left
with nothing pointing at it.

**Open session** needs no database picker: an odoo.sh build has exactly one
database and the instance names it. There is no listing here and no **↻** —
there is no `docker ps` for odoo.sh, so the only list that can exist is the one
you built.

Opening one is the longest wait on this screen — an SSH connection, then the
same registry load a container pays — so it shows the same things a container
start shows: the key goes inert with a spinning arrow, the note says what is
happening, and a log well under the card streams the build's own stderr as it
arrives. A failed open leaves that well up with the error on the note, and the
key comes back. Nothing about the wait is inferred from a timer.

Two things behave differently once such a session is open, both because it is
not your machine: **Grant commit** is enabled for you, not only for an agent,
and until you press it **Commit** is disabled rather than offered and refused.
On a `production` build the latch is disabled too and says why — nothing
grants a commit there. Rollback and running code are untouched.

### Server (SSH)

An Odoo installed directly on a server. Nothing about one can be discovered —
which user runs it, which interpreter, where `odoo-bin` and its config are — so
whoever can log in writes it down, in two fields and a few more:

- **Name** — what the card and its sessions are called.
- **Access** — how to arrive as the right user: `ssh -i ~/.ssh/server.pem
  ubuntu@host sudo -n -u odoo -H`. It is a *prefix*: it ends where a command may
  follow, which is why steps typed one after another into a terminal (`ssh`, then
  `sudo su`, then `su odoo`) become one line.
- **Launch** — what to run once there: the absolute path of the interpreter and of
  `odoo-bin`, then `shell` and whatever options the server needs. Run exactly as
  written. Add `--no-http` (or `--workers=0`): a server whose config has `workers`
  above 0 makes the shell bind its HTTP port first, and fails beside the running
  service with "Address already in use". The form warns when neither is there, and
  Probe says what the config will do.
- **Stage** — `production` (the default: only you commit, one commit at a time,
  after typing its name; an agent never writes there) or `staging` (commit off
  until you grant it). Nothing on a plain server says what it is, so you do.

There is no field for a database: name one with `-d NAME` in Launch, or leave it
out and the server's own `db_name` decides.

Under the fields the form says, in sentences, what the recipe means as it is
typed — who it logs in as, to where, with which key; which user it becomes; what
it runs; which database — and a fold shows the command as it will be assembled.
This is the part to read before saving, because the realistic way to get a wrong
recipe is to paste one. A field the grammar refuses gets its reason under it, in
red and specific ("-o ProxyCommand is not allowed: it runs a command on this
machine", "launch starts with an absolute path…"). Nothing typed is ever run on
this machine; the form shows the admin key's absence as one line with a button,
never a dialog in the middle of typing.

**Probe** runs the recipe once and says who it landed as, where, which Python and
Odoo, and whether the config is readable by that user; **Save** writes the card.
A card shows `user@host:port`, who it runs as, the stage, and an *untested* badge
for Odoo 13 and 14. **edit** puts it back in the form; **×** removes it, disabled
while a session is open on it. Opening one waits the same way an odoo.sh build
does, with the server's own stderr as progress. An unknown host key is refused
with what to do: connect once from a terminal and accept it.

Odoo 13 and 14 open with a warning and are not claimed — see
[architecture.md](architecture.md#target-discovery).

## Sessions

A tab strip of open sessions (`odoo19-dev / acme_dev`, or `36887345 / staging`
for an odoo.sh build), each closable. A
second session on the same container and database opens from the session
keyboard's **New** key without leaving the current tab — the twin shows up as
another tab. Idle tabs are cyan; the selected one is amber. While a test is
running, the session badge reads `testing` in Journals-warning rose, and that
session's tab grows a blinking lamp — a small rose dot, like a status light
(no blink if the OS asks for reduced motion). A short run still holds the
badge and the lamp for one pulse so they are readable. Ordinary `exec` does
not light the lamp and stays cyan `busy`.

Sessions opened by *anyone* — an agent through MCP, another browser tab —
appear here as they're opened, with no reload needed: the page keeps a socket
on the registry itself, not only on sessions it opened. A session someone
else owns opens in **observer mode**: you see its feed and its Odoo log tail
live, but the editor is hidden and there's nothing to type into. Opening the
log keeps the same gap under the session keyboard that the editor would have
left — it does not sit flush against the keys.

Under the title, the meta line is Odoo version, the session id (click to
copy), then the local start time and how many seconds the session has been
open — `14:42:07 (18s)`. The seconds tick in place; the rest of the header
does not redraw. Hover the clock for the same date stamp the Journals screen
uses. A session this tab opened stamps itself immediately; one that arrived
from a reload or from someone else waits for the journal's `opened_at`.

The state badge follows the session: `ready`, `busy`, `starting`, `dead`,
`closed`. While a test is running it reads `testing` instead of `busy`, in
the same rose as the Journals unmasked-warning banner. The blink lives on
the tab lamp, not on the badge. An ordinary `exec` stays cyan `busy`.
`watching · mcp-agent` is unchanged.

### Ownership

The header shows who owns the session: yours has the editor, someone else's
shows a `watching · <label>` badge. Two latches on the session keyboard
control this:

- **Grant access** hands the session to an agent (you keep watching; you
  stop typing) and hands it back the same way. On the way out, it copies
  `{"session_id", "write_key"}` — give that to the agent once. On the way
  back, no secret is needed: you already hold this session's key.
- **Grant commit** lets an agent's `commit` calls actually go through. It's
  only enabled while an agent owns the session, amber when granted, and
  asks for confirmation naming the database when turned on.

Closing or reclaiming a session you never owned yourself needs the daemon's
admin key — kept in `~/.odoo-sheller/admin.key`, and printed at startup when
the daemon runs in a terminal (never into a log file). A
browser tab only asks for it when the daemon actually refuses something, never
up front. The desktop app reads the file itself after the daemon is up, so
Grant access / Grant commit / take back do not prompt there. A session that's
already `dead` can be closed by anyone, no key required — there's no process
left to protect.

The tab's `×` does an ordinary **Close** (Odoo unwinds cleanly); `⌥`-click
forces a **Kill** instead. While either is in flight the tab reads
`closing…` / `killing…`; a close already under way can still be escalated to
a kill.

### Session keyboard

A compact two-row grid. Controls that don't apply right now are *disabled*,
never hidden — the layout doesn't jump around as state changes, and a hover
hint always explains why a key is inert.

| Key | What it does |
|---|---|
| **Grant commit** | See Ownership, above. On an odoo.sh session it is enabled for you as well, and disabled entirely on `production` |
| **Grant access** | See Ownership, above |
| **Close** | Graceful: the bootstrap leaves its loop, Odoo rolls back and closes the cursor, the process exits. If it hasn't exited after ten seconds — a long command can hold it open — the daemon escalates to a kill on its own |
| **Kill** | `SIGKILL` immediately, no waiting, no Odoo teardown. Postgres rolls back the transaction on its own. Journalled as `killed`, so an ordinary close is never confused with one that had to be forced |
| Either, on a session the daemon no longer has | Removes the tab. `session_gone` is the state Close was asking for, not a failure — and a daemon restart puts every open tab in that state at once |
| **Interrupt** | Enabled while `busy`. Sends `SIGINT` into the container; the command ends as `KeyboardInterrupt` and the session stays alive |
| **Rollback** | Discards the open transaction, invalidates the cache, leaves a marker in the feed |
| **Commit** | The only key that writes anything. Confirms first, naming the database |
| **New** | Opens another session on the same container and database as a new tab, without leaving this one |

There's no on-screen "run" key — `⌘+Enter` / `Ctrl+Enter` runs the buffer.
There's no "reconnect" either: a dead session is closed or left as a tab, and
a fresh one is opened with **New**.

### Editor

Python syntax highlighting, `⌘+Enter to run · ↑↓ history` stated right in the
corner. Up/down arrows walk previous commands when the caret is at the edge
of the buffer. **taller** / **shorter** doubles the editor's height for the
session (not remembered across a reload).

While a command runs, the editor stays usable for typing the next one, but
running it is refused until the session is `ready` again — visibly, before
you even try, never a silent no-op.

### Cell feed

One card per command, newest on top. Each card: the code, then stdout, the
returned value (the last expression's `repr`, if there was one), an error
with its traceback, and how long it took.

- Commands are numbered in the order you ran them — **#1 is the very first**,
  and it sits at the bottom. After a reload the numbers pick up where the
  journal left off, never resetting to 1.
- Click a card's header (not just the small fold dot) to collapse it to just
  that header, or expand it again. A card an *agent* ran starts collapsed;
  one you ran yourself starts open. The `CELLS` heading folds or unfolds
  every card at once. The feed scrolls without a visible scrollbar, so that
  fold control stays clickable.
- A running cell shows a spinner and a live elapsed-seconds counter — output
  for this version arrives in one piece at the end, so the counter is the
  only progress signal there is while it's still running. An empty feed
  during a test run does not offer `⌘+Enter`; the test-run card above it
  says what is running.
- While a test runs, a rose card sits above the cells: the test asked for
  (`account.TestAccountMoveReconcile`), the seconds since it started, the
  test running right now, and `started N · ✗ N fail · ! N error · ⏭ N
  skipped`. Fail and error turn red and bold once they are above zero. The
  card moves once per test, not per log line, stays visible with Logs open,
  and goes away when the run's result arrives — or when the session closes,
  and not a moment before: closing a session mid-run used to report it
  `ready` for an instant and take the card with it while the run went on. A
  class that fails in `setUpClass` counts as an error here too. Before the
  first test starts
  it reads `no test started yet — Odoo logs each one at INFO`: an Odoo
  configured above `INFO` never logs one, and then the card only shows the
  test asked for and the clock.
- A command restored from the journal whose original run blew its timeout is
  marked as a **late result**, so an abandoned command never quietly reads as
  an ordinary success.
- The card's header reads `#1 browser 17 Sep 16:36 / 0.00s / done`: which
  command it was, who ran it, when it was sent, how long it took, and how it
  ended. The last three are the ones separated by slashes; the date carries the
  full instant in its tooltip, and it survives a restore because the journal
  records it.
- Every card offers **copy code**, **copy output**, **edit** (puts that code
  back in the editor, to change and run again) and **re-run** (sends the same
  code as it stands, as a brand new command) — but only while the session is
  yours. Watching one an agent holds, none of the four is shown: two of them
  would be refused and the other two invite editing work that is not yours.
  Take the session back and they are there again, on every cell in the feed.
- Commit and rollback show up inline in the feed as markers, so the
  chronology makes clear which commands landed on which side of a write.

⌃⇧←/→ moves between the session tabs, wrapping around, and brings the sessions
screen with it. ⌘W closes the session in view, the same way the Close button does — the
confirmation included. In the desktop app the terminal dock binds the same key
for its own tabs, and the two never argue: they are separate documents, so
whichever has focus answers.

### Logs (Odoo's stderr)

A footer panel, collapsed by default, with a counter of lines that arrived
since it was last open — the counter turns red if any of them were warning
or error level.

Expanded, it takes the pane's leftover height and the lines scroll inside it.
The tail follows new lines only while you're already at the bottom —
scrolled up to read a traceback, it holds position until you scroll back
down yourself. A level filter replaces the counter while open. An
**expand** / **collapse** mark on the header's right edge hides both the
editor and the cell feed, giving the log the whole pane — reading a full
page of Odoo's output is the point, not the editor beside it — and brings
both back.

## Journals

Every past session, kept as a file, grouped by container and database. A
group currently holding a live session is marked `live`. Click a group
heading to expand or collapse its rows (groups start collapsed). The
heading reads `117 sessions · 99,756 / 30,761 KB · last …`: the size is the
sum of its rows.

Each row: timestamp, owner (`human`, `agent`, or `human→agent` for a
handover), session id, duration, command count, `committed` / `discarded`
(`committed` only for a commit that went through — a failed one wrote
nothing),
size — `772 / 243 KB`, records in the journal file and its size on disk,
never `0 KB` for a file with anything in it — and the row's own controls:
`.jsonl`, `.md`, a two-sheet copy glyph, and a trash icon. A
sticky header names every column; on a narrow window it's dropped and rows
wrap instead.

- Click anywhere on a row except a control to open that transcript.
- **.jsonl** / **.md** export the session; both confirm first, because
  **journals are unmasked** — see [security.md](security.md).
- The **copy** glyph puts the whole transcript on the clipboard, no
  confirmation, but the same warning applies: a clipboard is a way out of
  this machine too. It turns into a green tick once the copy lands, or red
  if it failed, and back after a moment.
- The trash icon deletes that journal file. It's hidden while the session is
  still live, and — because unlinking a file is the one truly irreversible
  thing this API does — it asks for the admin key the first time. A group
  heading gets its own trash once at least one row in it is finished: it
  confirms once, then deletes every non-live journal in the group, still
  running the rest of the batch even if one delete is refused.
- The open transcript has its own **expand** / **collapse** and a close mark
  to dismiss it and return to the full list.

## What the UI never leaves you guessing about

| Situation | What happens |
|---|---|
| Session starting | An explicit "loading Odoo registry" state — input stays disabled, nothing pretends to be ready early |
| Command running | Spinner and elapsed counter on the cell; Interrupt is available |
| A second command is attempted mid-run | Refused with "session busy"; what you typed is kept, not discarded |
| Interrupt used | The cell resolves as a `KeyboardInterrupt` error; the session stays ready |
| Command hit its timeout | The cell says so; the session stays `busy` until the abandoned command's real result finally arrives |
| Container process died | State goes `dead`; the last stderr lines are shown as the likely cause; open a replacement with **New** |
| Daemon unreachable | The whole UI shows a disconnected state — nothing is retried silently |
| Page reloaded | The last screen you were on comes back. Live sessions reattach as tabs, with their cell feed, editor history, and Odoo log rebuilt from that session's journal. Per-tab layout choices (taller editor, folded cards, log focus) reset; the journal list does not stay expanded |
| Close or Kill with pending work | Warns that uncommitted work will be discarded, whenever the pending-command count is above zero |

## Appearance, briefly

Dark, low-chroma palette; cyan marks the active tab, primary actions, and a
`ready`/`connected` state; amber marks the wordmark and a few destructive-ish
or "needs attention" states; green marks string literals in the editor. No
CDN, no web fonts, no remote images anywhere under `/web` — the whole page is
served by the daemon and works with the network off. The one exception in
this repository is `/docs` (Swagger UI), which is not under `/web` and needs
a CDN for its own bundle; see the [README](../README.md#api).
