# Documentation

Everything here assumes you've read the top-level [README](../README.md) —
install, quick start, the API reference, and the controls table live there.
This folder goes deeper on the parts worth explaining once, properly.

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | How odoo-sheller actually works: the three processes, the wire protocol between daemon and container, the session state machine, and the journal format |
| [ui-guide.md](ui-guide.md) | Using the web UI screen by screen — Connect, Sessions, Journals — with every control and what it guarantees |
| [agent-guide.md](agent-guide.md) | Giving an AI agent access through the MCP server: the tool list, ownership and handover from the agent's side, running it under Claude Desktop |
| [security.md](security.md) | The actual security model: what's protected, what deliberately isn't, and why journals aren't masked |
| [faq.md](faq.md) | Plain-language questions and answers, in English |
| [faq-ru.md](faq-ru.md) | То же самое по-русски |

## Where to start

- **Just want to run it?** The [README](../README.md) is enough — install,
  start the daemon, open the browser.
- **Want to understand what you're running before you trust it with a
  database?** [security.md](security.md), then [architecture.md](architecture.md).
- **Confused by something in the UI?** [ui-guide.md](ui-guide.md) documents
  every control; [faq.md](faq.md) / [faq-ru.md](faq-ru.md) answer the
  questions people actually ask.
- **Wiring up an agent?** [agent-guide.md](agent-guide.md).

[CHANGELOG.md](../CHANGELOG.md), at the repository root, tracks what shipped
in each version.
