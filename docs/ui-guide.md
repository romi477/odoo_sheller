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

A list of running containers, one card each. Probing happens automatically
when the screen loads, and each card also has its own re-probe for after a
restart or a config change.

- A container that already has a session shows a `connected · <database>`
  badge and offers **Close session** next to **Open session** — a *second*
  session on the same container, against another database, is entirely
  normal and allowed.
- **Open session** expands the card into a database picker. The default
  database comes from `db_name` in the container's `odoo.conf`, which in a
  dev container is almost always the right answer. If the database list
  couldn't be read at all, a free-text field takes its place.
- Probe failures are explained specifically — "no odoo-bin found in this
  container", "Odoo 17 found, only 19 is supported", "could not read database
  list — enter the name manually" — rather than as a generic error. A
  container that fails its probe keeps its card and its re-probe button; it
  just can't be opened until whatever's wrong is fixed.
- Opening a session takes a few seconds while Odoo loads its registry.
  **Start** goes inert with a spinning arrow, and a log well at the bottom of
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

## Sessions

A tab strip of open sessions (`odoo19-dev / acme_dev`), each closable. A
second session on the same container and database opens from the session
keyboard's **New** key without leaving the current tab — the twin shows up as
another tab.

Sessions opened by *anyone* — an agent through MCP, another browser tab —
appear here as they're opened, with no reload needed: the page keeps a socket
on the registry itself, not only on sessions it opened. A session someone
else owns opens in **observer mode**: you see its feed and its Odoo log tail
live, but the editor is hidden and there's nothing to type into.

Under the title, the meta line is Odoo version, the session id (click to
copy), then the local start time and how many seconds the session has been
open — `14:42:07 (18s)`. The seconds tick in place; the rest of the header
does not redraw. Hover the clock for the same date stamp the Journals screen
uses. A session this tab opened stamps itself immediately; one that arrived
from a reload or from someone else waits for the journal's `opened_at`.

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
admin key — printed at startup and kept in `~/.odoo-sheller/admin.key`. The
UI only asks for it when the daemon actually refuses something, never up
front. A session that's already `dead` can be closed by anyone, no key
required — there's no process left to protect.

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
| **Grant commit** | See Ownership, above |
| **Grant access** | See Ownership, above |
| **Close** | Graceful: the bootstrap leaves its loop, Odoo rolls back and closes the cursor, the process exits. If it hasn't exited after ten seconds — a long command can hold it open — the daemon escalates to a kill on its own |
| **Kill** | `SIGKILL` immediately, no waiting, no Odoo teardown. Postgres rolls back the transaction on its own. Journalled as `killed`, so an ordinary close is never confused with one that had to be forced |
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
  only progress signal there is while it's still running.
- A command restored from the journal whose original run blew its timeout is
  marked as a **late result**, so an abandoned command never quietly reads as
  an ordinary success.
- Every card offers **copy code**, **copy output**, **re-run** (sends the
  same code again as a brand new command).
- Commit and rollback show up inline in the feed as markers, so the
  chronology makes clear which commands landed on which side of a write.

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
heading to expand or collapse its rows (groups start collapsed).

Each row: timestamp, owner (`human`, `agent`, or `human→agent` for a
handover), session id, duration, command count, `committed` / `discarded`,
and the row's own controls — `.jsonl`, `.md`, `copy`, and a trash icon. A
sticky header names every column; on a narrow window it's dropped and rows
wrap instead.

- Click anywhere on a row except a control to open that transcript.
- **.jsonl** / **.md** export the session; both confirm first, because
  **journals are unmasked** — see [security.md](security.md).
- **copy** puts the whole transcript on the clipboard, no confirmation, but
  the same warning applies: a clipboard is a way out of this machine too.
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
