"""Checks on the served UI.

These do not run the UI — there is no JS toolchain in this project on purpose.
What they can prove honestly is the contract between the three files: the page
is self-contained, the editor is vendored, the assets are served, and every
static selector app.js reaches for actually exists in index.html. That last one
catches the failure this pairing really has: a class renamed in the markup,
leaving the script to throw at runtime with a green test suite.

Behaviour that needs a browser is verified by hand — see the plan, Task 8.
"""

import re
import tomllib
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odoo_sheller.api import create_app

WEB = Path(__file__).resolve().parent.parent / "odoo_sheller" / "web"
UNMASKED_WARNING = "unmasked"


class _Markup(HTMLParser):
    """Collects every class, id and data attribute in the page, templates included."""

    def __init__(self):
        super().__init__()
        self.classes: set[str] = set()
        self.ids: set[str] = set()
        self.data_attributes: set[str] = set()

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "class" and value:
                self.classes.update(value.split())
            elif name == "id" and value:
                self.ids.add(value)
            elif name.startswith("data-"):
                self.data_attributes.add(name)


@pytest.fixture(scope="module")
def markup() -> _Markup:
    parser = _Markup()
    parser.feed((WEB / "index.html").read_text(encoding="utf-8"))

    return parser


@pytest.fixture(scope="module")
def app_js() -> str:

    return (WEB / "app.js").read_text(encoding="utf-8")


def test_page_makes_no_external_requests():
    """The UI is self-contained: no CDN. The wordmark may link out to GitHub."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert not re.search(
        r"<(?:script|link|img|iframe|source)\b[^>]*(?:src|href)=[\"']https?://",
        html,
        re.IGNORECASE,
    )
    assert "http://" not in html
    assert re.findall(r'https://[^"\s>]+', html) == [
        "https://github.com/romi477/odoo_sheller"
    ]


def test_wordmark_carries_the_package_version(markup):
    """Version sits after odoo-sheller., as vX.Y.Z from pyproject, not a second brand mark."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    pyproject = tomllib.loads((WEB.parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]
    assert "app-mark" not in markup.classes
    assert f'class="wordmark">odoo-sheller.<sub>v{version}</sub>' in html
    assert re.search(
        r'<a[^>]*class="brand"[^>]*href="https://github.com/romi477/odoo_sheller"',
        html,
    )
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert ".app-mark" not in css
    sub = re.search(r"\.wordmark sub\s*\{([^}]*)\}", css)
    assert sub is not None
    assert "vertical-align" in sub.group(1)


def test_page_warns_that_journals_are_unmasked(markup):
    html = (WEB / "index.html").read_text(encoding="utf-8").lower()
    assert UNMASKED_WARNING in html, "the journals screen must carry a standing warning"
    assert "warning" in markup.classes


def test_unmasked_warning_is_pale_rose_not_amber():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    warning = re.search(r"(?<![a-z-])\.warning\s*\{([^}]*)\}", css)
    assert warning is not None
    assert "var(--red)" in warning.group(1)
    assert "var(--warning)" not in warning.group(1)
    assert "var(--amber)" not in warning.group(1)


def _classes_created_by_script(app_js: str) -> set[str]:
    """Classes app.js puts on elements it builds itself, so it may query them too."""
    created: set[str] = set()
    for value in re.findall(r"""class=["']([^"']+)["']""", app_js):
        # Template literals interpolate a state class after the static ones:
        # class="journal-owner ${owner.kind}" — keep the part that is fixed.
        created.update(value.split("${")[0].split())
    for value in re.findall(r"""className\s*=\s*[`'"]([^`'"{}]+)[`'"]""", app_js):
        created.update(value.split())
    for value in re.findall(r"""classList\.(?:add|toggle)\(\s*['"]([^'"]+)['"]""", app_js):
        created.add(value)

    return created


def test_every_static_selector_in_app_js_exists_in_the_markup(markup, app_js):
    """A renamed class in index.html must fail here, not silently at runtime."""
    selectors = re.findall(r"""querySelector(?:All)?\(\s*['"]([^'"]+)['"]\s*\)""", app_js)
    assert selectors, "expected app.js to query the DOM"

    known_classes = markup.classes | _classes_created_by_script(app_js)
    missing = []
    for selector in selectors:
        for token in re.findall(r"[.#][A-Za-z0-9_-]+", selector):
            name = token[1:]
            known = markup.ids if token[0] == "#" else known_classes
            if name not in known:
                missing.append(selector)

    assert not missing, f"selectors with no matching element anywhere: {sorted(set(missing))}"


def test_cell_timer_updates_without_a_full_rerender(app_js):
    """The elapsed counter must not redraw the panel: that drops the caret."""
    body = re.search(r"const timer = setInterval\(.*?\}, 250\);", app_js, re.DOTALL)
    assert body is not None
    assert "updateCellDuration" in body.group(0)
    assert "renderSessions" not in body.group(0)


def test_session_age_ticks_without_a_full_rerender(app_js):
    """A 1s header tick that called renderSessions would fight the editor the same way."""
    body = re.search(r"function tickSessionAges\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    assert "paintSessionAge" in body.group(0)
    assert "renderSessions" not in body.group(0)
    assert "setInterval(tickSessionAges" in app_js


def test_session_age_uses_history_or_a_local_stamp(app_js):
    """GET /api/sessions has no opened_at; history does, and a tab that opened it knows when."""
    history = re.search(r"function applyHistory\(.*?\n\}", app_js, re.DOTALL)
    assert history is not None
    assert "opened_at" in history.group(0)
    assert "openedAt" in history.group(0)
    attach = re.search(r"function attachSession\(.*?\n\}", app_js, re.DOTALL)
    assert attach is not None
    assert "openedAt" in attach.group(0)
    assert "reattached" in attach.group(0)


def test_editor_is_refreshed_after_being_reattached(app_js):
    assert "record.editor?.refresh()" in app_js


def test_abandoning_the_database_picker_collapses_the_card(app_js):
    """Open session expands the picker; a click on the card outside it puts the card back."""
    assert "function closeAllPickers(" in app_js
    closer = re.search(r"function closeAllPickers\(.*?\n\}", app_js, re.DOTALL)
    assert closer is not None
    assert "picker.hidden = true" in closer.group(0)
    opener = re.search(r"function openPicker\(.*?\n\}", app_js, re.DOTALL)
    assert opener is not None
    assert "closeAllPickers(" in opener.group(0)
    render = re.search(r"function renderContainers\(.*?\n\}", app_js, re.DOTALL)
    assert render is not None
    body = render.group(0)
    assert "closest('.picker')" in body
    assert "closest('.open')" in body
    assert "closeAllPickers(" in body


def test_run_is_disabled_unless_the_session_is_ready(app_js):
    assert "rollback.disabled = !accepting" in app_js
    assert "commit.disabled = !accepting" in app_js
    assert "record.info.state === 'ready'" in app_js
    assert "'Cmd-Enter'" in app_js
    assert "'Ctrl-Enter'" in app_js


def test_reattached_sessions_ask_the_daemon_for_their_history(app_js):
    """Wiring, not wording: reattach must pull the feed back from the journal."""
    reattach = re.search(r"async function reattachSessions\(.*?\n\}", app_js, re.DOTALL)
    assert reattach is not None
    assert "loadSessionHistory" in reattach.group(0)

    loader = re.search(r"async function loadSessionHistory\(.*?\n\}", app_js, re.DOTALL)
    assert loader is not None
    assert "/api/sessions/${id}/history" in loader.group(0)


def test_session_id_button_copies_the_id(app_js, markup):
    assert "session-id" in markup.classes
    handler = re.search(
        r"sessionId\.addEventListener\('click'.*?\n    \}\);", app_js, re.DOTALL
    )
    assert handler is not None
    assert "copyText(id)" in handler.group(0)


def test_a_log_line_does_not_redraw_the_whole_session(app_js):
    """Odoo emits hundreds of lines at startup; each one must stay cheap."""
    handler = re.search(
        r"socket\.addEventListener\('message'.*?\n  \}\);", app_js, re.DOTALL
    )
    assert handler is not None
    stderr_branch = handler.group(0).split("message.kind === 'stderr'")[1]
    assert "appendLogLine" in stderr_branch
    assert "renderSessions" not in stderr_branch.split("return;")[0]


def test_log_tail_follows_only_when_the_reader_is_at_the_bottom(app_js):
    assert "function isLogPinned" in app_js
    for name in ("renderLogs", "appendLogLine"):
        body = re.search(rf"function {name}\(.*?\n\}}", app_js, re.DOTALL)
        assert body is not None, name
        assert "isLogPinned" in body.group(0), name
        assert re.search(r"if \(pinned\) \{", body.group(0)), name


def test_log_focus_hides_the_editor_and_cell_feed(app_js, markup):
    """Focus is for reading the log at full size: neither the editor nor the feed belongs there."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "logs-focus" in markup.classes
    assert html.find("logs-head-end") < html.find('class="logs-focus')
    assert html.find('class="log-filter"') < html.find('class="logs-focus')
    button = re.search(
        r'<button[^>]*class="[^"]*logs-focus[^"]*"[^>]*>\s*</button>', html
    )
    assert button is not None
    assert "fold-mark" in button.group(0)
    assert re.search(r'class="logs-focus"[^>]*\bhidden\b', html) or "hidden" in button.group(0)
    css = (WEB / "style.css").read_text(encoding="utf-8")
    hidden_feed = re.search(r"\.session\.logs-focused\s+\.feed\s*\{([^}]*)\}", css)
    assert hidden_feed is not None
    assert "display: none" in hidden_feed.group(1)
    hidden_editor = re.search(r"\.session\.logs-focused\s+\.editor-pane\s*\{([^}]*)\}", css)
    assert hidden_editor is not None, "the editor must go away too, not just the feed"
    assert "display: none" in hidden_editor.group(1)
    # .logs's own `margin-top: auto` resolves to 0 once flex-grow claims all the
    # free space, so with the editor gone the log's top border would land right
    # under the session keyboard instead of where the editor's did.
    logs_margin = re.search(r"\.session\.logs-focused\s+\.logs\s*\{([^}]*)\}", css)
    assert logs_margin is not None, "the log must restate the editor's own top margin"
    assert "margin-top: var(--session-stack)" in logs_margin.group(1)
    assert "logsFocused: false" in app_js
    bind = re.search(r"function bindLogs\(.*?\n\}", app_js, re.DOTALL)
    assert bind is not None
    assert ".logs-focus" in bind.group(0)
    assert "logsFocused" in bind.group(0)
    render = re.search(r"function renderLogs\(.*?\n\}", app_js, re.DOTALL)
    assert render is not None
    assert "logs-focused" in render.group(0)
    assert "focus.hidden" in render.group(0)
    assert "classList.toggle('expand'" in render.group(0)
    assert "classList.toggle('collapse'" in render.group(0)
    assert "show cells" not in render.group(0)
    assert "focus.textContent" not in render.group(0)


def test_unseen_log_tail_is_empty_when_nothing_is_unseen(app_js):
    """slice(-0) returns the whole array — the empty case must be explicit."""
    body = re.search(r"function updateLogCount\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    assert "record.unseenLogs ?" in body.group(0)


def test_log_bar_chrome_is_amber():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    toggle = re.search(r"\.logs-toggle\s*\{([^}]*)\}", css)
    assert toggle is not None
    assert "var(--amber)" in toggle.group(1)
    # The new-line count is white, not amber: it reads as a plain number, and
    # the alert variant below still turns it red for an unseen warning/error.
    count = re.search(r"(?m)^\.log-count\s*\{([^}]*)\}", css)
    assert count is not None
    assert "oklch(1 0 0)" in count.group(1)
    alert = re.search(r"\.log-count\.alert\s*\{([^}]*)\}", css)
    assert alert is not None
    assert "var(--red)" in alert.group(1)
    filt = re.search(r"\.log-filter\s*\{([^}]*)\}", css)
    assert filt is not None
    assert "var(--amber)" in filt.group(1)
    assert "border: 0" in filt.group(1)
    assert "120px" not in filt.group(1)
    assert "min-width: 0" in filt.group(1)
    assert re.search(r"width:\s*calc\(11ch", filt.group(1))
    # Anchored at line start: a lookbehind alone also matched the compound
    # `.session.logs-focused .logs` rule, since the character right before
    # `.logs` there is just a space, not one of the excluded ones.
    logs = re.search(r"(?m)^\.logs\s*\{([^}]*)\}", css)
    assert logs is not None
    assert "1px solid var(--chrome-edge)" in logs.group(1)
    assert "border-strong" not in logs.group(1)


def test_open_logs_show_a_close_mark_and_white_type(app_js, markup):
    """Open logs: × to close, white type. The label itself stays amber, open or not."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "logs-close" in markup.classes
    close = re.search(
        r'<button[^>]*class="[^"]*logs-close[^"]*"[^>]*>×</button>', html
    )
    assert close is not None
    assert "hidden" in close.group(0)
    render = re.search(r"function renderLogs\(.*?\n\}", app_js, re.DOTALL)
    assert render is not None
    assert "logs-close" in render.group(0)
    assert "close.hidden" in render.group(0)
    css = (WEB / "style.css").read_text(encoding="utf-8")
    lines = re.search(r"\.session\.logs-open\s+\.log-lines\s*\{([^}]*)\}", css)
    assert lines is not None
    assert "oklch(1 0 0)" in lines.group(1)
    toggle = re.search(r"(?m)^\.logs-toggle\s*\{([^}]*)\}", css)
    assert toggle is not None
    assert "var(--amber)" in toggle.group(1)
    assert re.search(r"\.session\.logs-open\s+\.logs-toggle[^{]*\{", css) is None, (
        "the label must not turn white when the log opens"
    )
    filt = re.search(r"\.session\.logs-open\s+\.log-filter[^{]*\{([^}]*)\}", css)
    assert filt is not None
    assert "oklch(1 0 0)" in filt.group(1)
    assert "border-color" not in filt.group(1)


def test_open_logs_fill_to_the_editor_when_the_feed_is_empty():
    """No cells: logs meet the editor. Many cells: the feed keeps a 40% strip."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    capped = re.search(r"\.session\.logs-open \.feed\s*\{([^}]*)\}", css)
    assert capped is not None
    assert "max-height: 40%" in capped.group(1)
    empty = re.search(
        r"\.session\.logs-open \.feed:has\(\.empty\)\s*\{([^}]*)\}", css
    )
    assert empty is not None
    assert "display: none" in empty.group(1)


def test_ui_help_lives_in_the_markup(app_js, markup):
    """One place per control: no parallel table of titles in the script."""
    assert "BUTTON_HELP" not in app_js
    assert "applyButtonHelp" not in app_js
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for control in ('class="session-key commit"', 'class="session-key kill"', 'class="reprobe"'):
        index = html.index(control)
        assert "title=" in html[index:html.index(">", index)], control


def test_stylesheet_is_linked_without_a_hand_maintained_cache_buster():
    """Assets are served no-store; a ?v= tag is dead weight nobody will bump."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'href="/static/style.css"' in html
    assert 'src="/static/app.js"' in html


def test_toolbar_is_a_separate_rounded_bar():
    """macOS-style chrome: tabs sit in the middle of a bar that is not the body."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert html.index('id="top"') < html.index('class="shell"')
    assert 'class="nav-tabs"' in html
    assert 'class="app"' in html
    css = (WEB / "style.css").read_text(encoding="utf-8")
    app = re.search(r"\.app\s*\{([^}]*)\}", css)
    assert app is not None
    assert "gap:" in app.group(1)
    top = re.search(r"#top\s*\{([^}]*)\}", css)
    assert top is not None
    assert "border-radius:" in top.group(1)
    assert "grid-template-columns:" in top.group(1)
    assert "box-shadow:" in top.group(1)
    assert "var(--chrome-edge)" in top.group(1)
    chrome = re.search(r"--chrome-edge:\s*([^;]+);", css)
    assert chrome is not None
    assert "1px" not in chrome.group(1)
    assert "var(--cyan)" in chrome.group(1)
    assert "38%" in chrome.group(1)
    active = re.search(r"#top button\.active\s*\{([^}]*)\}", css)
    assert active is not None
    assert "color: var(--cyan)" in active.group(1)
    assert "var(--cyan-dark)" not in active.group(1)
    assert "#top button.active::after" not in css
    press = re.search(
        r"button:active:not\(:disabled\)\s*\{([^}]*)\}", css
    )
    assert press is not None
    assert "translateY" in press.group(1)
    assert "inset" in press.group(1)
    assert "a.brand:active" in css
    assert "#api-docs:active" in css
    assert ".journal-export-links a:active" in css
    lift = re.search(
        r"\.nav-tabs button:hover:not\(:disabled\)\s*\{([^}]*)\}", css
    )
    assert lift is not None
    assert "translateY(-" in lift.group(1)
    assert "box-shadow" not in lift.group(1)


def test_toolbar_tabs_hover_amber():
    """Any of Connect / Sessions / Journals turns amber on hover, not only the current tab."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    hover = re.search(
        r"#top button:hover:not\(:disabled\)\s*\{([^}]*)\}", css
    )
    assert hover is not None
    assert "color: var(--amber)" in hover.group(1)
    muted = re.search(r"#top button\s*\{([^}]*)\}", css)
    assert muted is not None
    assert "color: var(--muted)" in muted.group(1)


def test_screens_share_a_sixteen_pixel_start():
    """Connect, Sessions and Journals start on the same line; switching tabs must not jump."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert "--screen-start: 16px" in css
    screens = re.findall(r"(?<![.#\w-])\.screen\s*\{([^}]*)\}", css)
    assert screens
    for body in screens:
        assert "var(--screen-start)" in body
    sessions = re.search(r"#screen-sessions\s*\{([^}]*)\}", css)
    assert sessions is not None
    assert "var(--screen-start)" in sessions.group(1)
    assert re.search(
        r"padding:\s*var\(--screen-start\)\s+var\(--screen-gutter\)\s+0",
        sessions.group(1),
    )


def test_screens_share_a_forty_pixel_gutter():
    """Connect, Journals and Sessions share one column; the tab rule stops at its edges."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert "--screen-gutter: 40px" in css
    screens = re.findall(r"(?<![.#\w-])\.screen\s*\{([^}]*)\}", css)
    assert screens
    for body in screens:
        assert "var(--screen-gutter)" in body
        assert "8vw" not in body
    sessions = re.search(r"#screen-sessions\s*\{([^}]*)\}", css)
    assert sessions is not None
    assert "var(--screen-gutter)" in sessions.group(1)
    workspace = re.search(r"(?<![.#\w-])\.session\s*\{([^}]*)\}", css)
    assert workspace is not None
    assert "var(--screen-gutter)" not in workspace.group(1)
    listed = re.search(r"\.session-tab-list\s*\{([^}]*)\}", css)
    assert listed is not None
    assert "10px" not in listed.group(1)
    editor = re.search(r"\.editor-pane\s*\{([^}]*)\}", css)
    assert editor is not None
    assert "16px" not in editor.group(1)
    feed = re.search(r"(?<![.#\w-])\.feed\s*\{([^}]*)\}", css)
    assert feed is not None
    assert "16px" not in feed.group(1)


def test_start_button_is_amber_label_with_cyan_hover_ring():
    """Rest: no outline. Hover: cyan outline, label stays amber."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    start = re.search(r"(?<![.#\w-])\.primary\s*\{([^}]*)\}", css)
    assert start is not None
    assert "var(--amber)" in start.group(1)
    assert "border-color: transparent" in start.group(1)
    assert "var(--cyan)" not in start.group(1)
    hover = re.search(
        r"\.primary:hover:not\(:disabled\)\s*\{([^}]*)\}", css
    )
    assert hover is not None
    assert "var(--cyan)" in hover.group(1)
    assert "var(--amber)" in hover.group(1)


def test_open_session_is_amber_label_with_cyan_hover_ring():
    """Rest: no extra ring. Hover: cyan outline, label stays amber."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    open_btn = re.search(r"(?<![.#\w-])\.open\s*\{([^}]*)\}", css)
    assert open_btn is not None
    assert "var(--amber)" in open_btn.group(1)
    assert "var(--cyan)" not in open_btn.group(1)
    hover = re.search(r"\.open:hover:not\(:disabled\)\s*\{([^}]*)\}", css)
    assert hover is not None
    assert "var(--cyan)" in hover.group(1)
    assert "var(--amber)" in hover.group(1)


def test_connected_badge_sits_with_the_container_name():
    """The badge rides on the name line, a double space after the container name."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    template = html.split('id="container-card"', 1)[1].split("</template>", 1)[0]
    identity = re.search(r'<div class="identity">(.*?)</div>', template, re.DOTALL)
    assert identity is not None
    block = identity.group(1)
    assert 'class="name-line"' in block
    assert 'class="badge connected"' in block
    assert block.find("name mono") < block.find("badge connected")
    css = (WEB / "style.css").read_text(encoding="utf-8")
    line = re.search(r"\.name-line\s*\{([^}]*)\}", css)
    assert line is not None
    assert "display: flex" in line.group(1)
    assert "2ch" in line.group(1)
    assert ".card:has(.connected:not([hidden])) .identity" not in css
    assert ".card:has(.connected:not([hidden])) .connected" not in css


def test_container_facts_sit_together_below_the_name():
    """Image/uptime and the probe sit as one block under the name, not inside it."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    template = html.split('id="container-card"', 1)[1].split("</template>", 1)[0]
    identity = re.search(r'<div class="identity">(.*?)</div>', template, re.DOTALL)
    assert identity is not None
    assert "class=\"meta\"" not in identity.group(1)
    facts = re.search(r'<div class="card-facts">(.*?)</div>', template, re.DOTALL)
    assert facts is not None
    block = facts.group(1)
    assert 'class="meta"' in block
    assert "probe-note" in block
    assert block.find("meta") < block.find("probe-note")
    assert template.find("card-facts") < template.find("class=\"picker\"")
    css = (WEB / "style.css").read_text(encoding="utf-8")
    group = re.search(r"\.card-facts\s*\{([^}]*)\}", css)
    assert group is not None
    assert "margin-top" in group.group(1)
    assert "gap: 0" in group.group(1)
    inner = re.search(r"\.card-facts \.meta,\s*\.card-facts \.probe-note\s*\{([^}]*)\}", css)
    assert inner is not None
    assert "margin: 0" in inner.group(1)


def test_connected_card_and_ready_badge_use_cyan_not_green():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    card = re.search(
        r"\.card:has\(\.connected:not\(\[hidden\]\)\)\s*\{([^}]*)\}", css
    )
    assert card is not None
    assert "var(--cyan)" in card.group(1)
    assert "var(--green)" not in card.group(1)
    badge = re.search(
        r"\.badge\.ready,\s*\.badge\.connected,\s*\.badge\.committed\s*\{([^}]*)\}",
        css,
    )
    assert badge is not None
    assert "var(--cyan)" in badge.group(1)
    assert "var(--green)" not in badge.group(1)
    status = re.search(r"\.journal-status\.committed\s*\{([^}]*)\}", css)
    assert status is not None
    assert "var(--cyan)" in status.group(1)
    assert "var(--green)" not in status.group(1)
    commit_mark = re.search(r"\.boundary\.commit\s*\{([^}]*)\}", css)
    assert commit_mark is not None
    assert "var(--cyan)" in commit_mark.group(1)
    assert "var(--green)" not in commit_mark.group(1)
    rollback_mark = re.search(r"\.boundary\.rollback\s*\{([^}]*)\}", css)
    assert rollback_mark is not None
    assert "var(--amber)" in rollback_mark.group(1)
    live = re.search(r"\.journal-live\s*\{([^}]*)\}", css)
    assert live is not None
    assert "var(--cyan)" in live.group(1)
    assert "var(--green)" not in live.group(1)


def test_journal_transcript_is_outlined_in_cyan():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    view = re.search(r"#journal-view\s*\{([^}]*)\}", css)
    assert view is not None
    assert "--cyan" in view.group(1)


def test_ui_assets_tell_the_browser_not_to_cache_them():
    with TestClient(create_app()) as client:
        for path in ("/web", "/static/style.css", "/static/app.js"):
            assert client.get(path).headers["cache-control"] == "no-store"


def test_session_tabs_are_chrome_islands(app_js):
    """A rule under every tab; the active one sits on it, top-rounded, square below."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    strip = re.search(r"#session-tabs\s*\{([^}]*)\}", css)
    assert strip is not None
    body = strip.group(1)
    assert "align-items: stretch" in body
    assert "border-bottom:" in body
    assert "1px solid var(--chrome-edge)" in body
    assert re.search(r"padding:[^;]*\s0;", body)
    assert "overflow-x: auto" not in body
    tab = re.search(r"\.session-tab\s*\{([^}]*)\}", css)
    assert tab is not None
    assert "8px 8px 0 0" in tab.group(1)
    assert "color: var(--cyan)" in tab.group(1)
    assert "var(--muted)" not in tab.group(1)
    active = re.search(r"\.session-tab\.active\s*\{([^}]*)\}", css)
    assert active is not None
    assert "color: var(--amber)" in active.group(1)
    assert "border-color: var(--chrome-edge)" in active.group(1)
    assert "margin-bottom: -1px" in active.group(1)
    scoop = re.search(r"\.session-tab\.active::before\s*\{([^}]*)\}", css)
    assert scoop is not None
    assert "radial-gradient" in scoop.group(1)
    listed = re.search(r"\.session-tab-list\s*\{([^}]*)\}", css)
    assert listed is not None
    assert "overflow-x: auto" in listed.group(1)
    assert "session-tab-list" in app_js
    assert "tabs.replaceChildren(list)" in app_js
    assert "tabs.replaceChildren(list, add)" not in app_js
    assert "new-session" not in app_js
    assert ".new-session" not in css


def test_the_cells_heading_folds_every_card_to_its_header(app_js):
    """Collapse all leaves headers visible; it does not hide the feed."""
    assert "feedCollapsed" not in app_js
    assert "cards.hidden" not in app_js
    handler = re.search(
        r"querySelector\('\.feed-fold'\)\.addEventListener\('click'.*?\n    \}\);",
        app_js,
        re.DOTALL,
    )
    assert handler is not None
    assert "cell.collapsed" in handler.group(0)
    render = re.search(r"function renderFeed\(.*?\n\}", app_js, re.DOTALL)
    assert render is not None
    assert "Collapse all" in render.group(0)
    assert "Expand all" in render.group(0)
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert ".session.feed-collapsed" not in css
    head = re.search(r"\.feed-head\s*\{([^}]*)\}", css)
    assert head is not None
    assert "var(--cyan)" in head.group(1)


def test_the_whole_cell_header_folds_the_card(app_js):
    """The dot is the affordance; the header around it is the target."""
    render = re.search(r"function renderFeed\(.*?\n\}", app_js, re.DOTALL)
    assert render is not None
    body = render.group(0)
    handler = re.search(
        r"querySelector\('\.cell-head'\)\.addEventListener\('click'.*?\n    \}\);",
        body,
        re.DOTALL,
    )
    assert handler is not None
    assert "cell.collapsed = !cell.collapsed" in handler.group(0)
    assert ".cell-actions" in handler.group(0), "copy and re-run must not fold the card"
    # The dot sits inside the header, so a second listener would fire twice.
    assert "querySelector('.cell-fold').addEventListener" not in body
    css = (WEB / "style.css").read_text(encoding="utf-8")
    rules = {
        selector.strip(): body
        for selector, body in re.findall(r"([^{}]*)\{([^{}]*)\}", css)
    }
    assert "cursor: pointer" in rules[".cell-head"]
    actions = re.search(r"\.cell-actions\s*\{([^}]*)\}", css)
    assert actions is not None
    assert "cursor: default" in actions.group(1), "the gaps between buttons are not a fold"
    buttons = re.search(r"\.cell-actions button\s*\{([^}]*)\}", css)
    assert buttons is not None
    assert "border: 0" in buttons.group(1)
    hover = re.search(
        r"\.cell-actions button:hover:not\(:disabled\)\s*\{([^}]*)\}", css
    )
    assert hover is not None
    assert "border-color: transparent" in hover.group(1)


def test_the_fold_dot_keeps_its_two_colours(app_js):
    """Amber means it folds, cyan means it opens — the header click must not grey it."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    collapse = re.search(r"\.fold-mark\.collapse\s*\{([^}]*)\}", css)
    expand = re.search(r"\.fold-mark\.expand\s*\{([^}]*)\}", css)
    assert collapse is not None and expand is not None
    assert "var(--amber)" in collapse.group(1)
    assert "var(--cyan)" in expand.group(1)
    assert "var(--green)" not in expand.group(1)
    mark = re.search(r"\.fold-mark\.expand::before,\s*\.fold-mark\.expand::after\s*\{([^}]*)\}", css)
    assert mark is not None
    assert "var(--cyan" in mark.group(1)
    assert "var(--green" not in mark.group(1)
    render = re.search(r"function renderFeed\(.*?\n\}", app_js, re.DOTALL).group(0)
    assert "classList.add(cell.collapsed ? 'expand' : 'collapse')" in render


def test_agent_authored_cells_start_collapsed(app_js):
    """An agent filling the feed must not open every card; a human command still does."""
    restore = re.search(r"function cellsFromHistory\(.*?\n\}", app_js, re.DOTALL)
    assert restore is not None
    body = restore.group(0)
    assert "function cellsFromHistory(data, previous)" in body
    assert "actor.kind === 'agent'" in body
    assert "folds.has(entry.ordinal)" in body
    apply = re.search(r"function applyHistory\(.*?\n\}", app_js, re.DOTALL)
    assert apply is not None
    assert "cellsFromHistory(data, record.cells)" in apply.group(0)
    run = re.search(
        r"async function runCommand\(.*?const cell = \{.*?\n  \};",
        app_js,
        re.DOTALL,
    )
    assert run is not None
    assert "collapsed: false" in run.group(0)


def test_cell_head_leads_with_ordinal_then_status(app_js):
    """Identity first, outcome last: #1 browser 0.04s / done."""
    render = re.search(r"function renderFeed\(.*?\n\}", app_js, re.DOTALL)
    assert render is not None
    head = re.search(r'<header class="cell-head">(.*?)</header>', render.group(0), re.DOTALL)
    assert head is not None
    order = re.findall(r'class="(cell-[a-z]+)', head.group(1))
    assert order[:5] == [
        "cell-fold",
        "cell-ordinal",
        "cell-actor",
        "cell-duration",
        "cell-status",
    ]
    duration_at = head.group(1).find("cell-duration")
    status_at = head.group(1).find("cell-status")
    assert "/" in head.group(1)[duration_at:status_at]


def test_clipboard_failure_does_not_touch_the_offline_banner(app_js):
    body = re.search(r"async function copyText\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    assert "setOffline" not in body.group(0)


def test_caret_and_selection_are_visible_on_the_dark_background():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    cursor = re.search(r"\.CodeMirror-cursor\s*\{([^}]*)\}", css)
    assert cursor is not None, "CodeMirror's default caret is black and invisible here"
    assert "var(--" in cursor.group(1)
    assert re.search(r"\.CodeMirror-selected\s*\{", css) is not None


def test_editor_height_toggle_has_no_outline():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    toggle = re.search(r"\.editor-height\s*\{([^}]*)\}", css)
    assert toggle is not None
    assert "border: 0" in toggle.group(1)
    hover = re.search(
        r"\.editor-height:hover:not\(:disabled\)\s*\{([^}]*)\}", css
    )
    assert hover is not None
    assert "border-color: transparent" in hover.group(1)


def test_vendored_editor_is_present():
    assert (WEB / "vendor" / "codemirror.min.js").stat().st_size > 10_000
    assert (WEB / "vendor" / "python.min.js").exists()


def test_assets_are_served():
    with TestClient(create_app()) as client:
        assert client.get("/web").status_code == 200
        assert client.get("/", follow_redirects=False).status_code == 307
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/style.css").status_code == 200
        logo = client.get("/static/logo.svg")
        assert logo.status_code == 200
        assert "image/svg+xml" in logo.headers["content-type"]
        assert client.get("/favicon.ico").status_code == 200
        assert client.get("/vendor/codemirror.min.js").status_code == 200


def test_page_uses_the_local_svg_as_favicon():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'rel="icon"' in html
    assert 'href="/static/logo.svg"' in html
    assert (WEB / "logo.svg").read_text(encoding="utf-8").lstrip().startswith("<svg")


def test_nav_links_to_swagger_docs():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'href="/docs"' in html
    assert 'id="api-docs"' in html


def test_swagger_link_hovers_amber():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    hover = re.search(r"#api-docs:hover\s*\{([^}]*)\}", css)
    assert hover is not None
    assert "color: var(--amber)" in hover.group(1)


def markup_or_script_classes(markup, app_js):

    return markup.classes | _classes_created_by_script(app_js)


def test_journal_rows_lead_with_time_owner_and_id(app_js, markup):
    """Who ran a session and which one it was are the first things you look for."""
    row = re.search(r"row\.innerHTML = `(.*?)`;", app_js, re.DOTALL)
    assert row is not None
    columns = re.findall(r'<span class="([a-z-]+)', row.group(1))
    assert columns[:3] == ["journal-stamp", "journal-owner", "journal-id"]
    assert "journal-status" in columns
    assert "journal-owner" in markup_or_script_classes(markup, app_js)


def test_a_refused_journal_delete_says_so(app_js):
    """A 409 from a live session must not read as a click that did nothing."""
    single = re.search(r"async function deleteJournal\(.*?\n\}", app_js, re.DOTALL)
    assert single is not None
    assert "catch" in single.group(0), "the trash button discards the promise"
    assert "alert(" in single.group(0)
    assert "authHeaders" in single.group(0), "the endpoint takes the admin key"
    # withAdminRetry, not a bare request: the key is asked for once, on refusal.
    assert "withAdminRetry" in single.group(0)
    group = re.search(r"async function deleteJournalGroup\(.*?\n\}", app_js, re.DOTALL)
    assert group is not None
    # One report for the batch, not one dialog per id.
    assert "failures" in group.group(0)
    assert "alert(" in group.group(0)


def test_the_journal_count_rides_in_the_heading(app_js, markup):
    """Journals / 42 rows — one title line, not a heading and a stray count above."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert re.search(
        r'<h1>Journals\s*<span class="journal-index-count" hidden></span></h1>', html
    )
    assert "journal-index-head" not in markup.classes
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert ".journal-index-head" not in css
    rule = re.search(r"\.journal-index-count\s*\{([^}]*)\}", css)
    assert rule is not None
    # The heading is 17px and bold; the count must not inherit either.
    assert "font-size: 12px" in rule.group(1)
    assert "font-weight: 400" in rule.group(1)
    layout = re.search(r"function applyJournalLayout\(.*?\n\}", app_js, re.DOTALL)
    assert layout is not None
    assert "count.hidden = !rows" in layout.group(0), "an empty list has no badge"
    assert "row${rows === 1 ? '' : 's'}" in layout.group(0)


def test_the_journal_list_names_its_columns(app_js, markup):
    """Seven columns of mono text say nothing about themselves without a header."""
    header = re.search(r"function journalColumns\(.*?\n\}", app_js, re.DOTALL)
    assert header is not None
    labels = re.findall(r"<span[^>]*>([a-z ]+)</span>", header.group(0))
    assert labels == [
        "opened", "owner", "session", "duration", "commands", "outcome", "export",
    ]
    assert "journal-delete-col" in header.group(0)
    assert "journal-columns" in markup_or_script_classes(markup, app_js)
    css = (WEB / "style.css").read_text(encoding="utf-8")
    # The header only stays aligned by sharing the row's grid — in every layout,
    # including the narrow one, which drops columns.
    tracks = [
        selector for selector, body in re.findall(r"([^{}]*)\{([^{}]*)\}", css)
        if "grid-template-columns" in body and ".journal-row" in selector
    ]
    assert tracks, "the row grid must be findable"
    for selector in tracks:
        assert ".journal-columns" in selector, selector.strip()
    pinned = [
        body for selector, body in re.findall(r"([^{}]*)\{([^{}]*)\}", css)
        if ".journal-columns" in selector and "position: sticky" in body
    ]
    assert pinned, "the header must survive scrolling"
    # Rows scroll under a sticky header; a transparent one shows them through.
    assert "background:" in pinned[0]


def test_the_narrow_layout_drops_the_column_header(app_js):
    """Below 760px the row wraps onto two lines; a header there labels nothing."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    narrow = re.search(r"@media \(max-width: 760px\) \{(.*?)\n\}", css, re.DOTALL)
    assert narrow is not None
    block = re.sub(r"/\*.*?\*/", "", narrow.group(1), flags=re.DOTALL)
    rules = [
        body for selector, body in re.findall(r"([^{}]*)\{([^{}]*)\}", block)
        if selector.strip() == ".journal-columns"
    ]
    assert rules, "the header needs a narrow-layout rule of its own"
    assert "display: none" in rules[0]


def test_the_journal_column_header_is_not_a_clickable_row(app_js):
    """It must not be counted as a journal, nor answer to a preview click."""
    body = re.search(r"function journalColumns\(.*?\n\}", app_js, re.DOTALL).group(0)
    assert "addEventListener" not in body
    assert "journal-row" not in body


def test_a_journal_without_an_owner_is_not_claimed_for_anyone(app_js):
    """Journals written before ownership existed must not read as human-run."""
    body = re.search(r"function journalOwner\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    assert "'unknown'" in body.group(0)
    assert "'shared'" in body.group(0), "a handover must be visible in the list"


def test_focus_mode_hides_the_index_and_keeps_the_title(app_js, markup):
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "journal-view-title" in markup.classes
    assert "journal-focus" in markup.classes
    focus = re.search(
        r'<button[^>]*class="[^"]*journal-focus[^"]*"[^>]*>\s*</button>', html
    )
    assert focus is not None
    assert "fold-mark" in focus.group(0)
    close = re.search(
        r'<button[^>]*class="[^"]*journal-close[^"]*"[^>]*>', html
    )
    assert close is not None
    assert "fold-mark" not in close.group(0)
    body = re.search(r"function setJournalFocus\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    assert "journal-focused" in body.group(0)
    assert "classList.toggle('expand'" in body.group(0)
    assert "classList.toggle('collapse'" in body.group(0)
    assert "textContent" not in body.group(0)
    assert "show list" not in body.group(0)

    css = (WEB / "style.css").read_text(encoding="utf-8")
    hidden = re.search(
        r"#screen-journals\.journal-focused[^{]*\{\s*display: none", css, re.DOTALL
    )
    assert hidden is not None, "focus mode must hide the list, not just shrink it"


def test_journal_groups_start_collapsed(app_js):
    """Groups open folded; expanding one is remembered until a full reload."""
    assert "expandedJournalGroups" in app_js
    assert "collapsedJournalGroups" not in app_js
    load = re.search(r"async function loadJournals\(.*?\n\}", app_js, re.DOTALL)
    assert load is not None
    assert "toggleJournalGroup" in load.group(0)
    assert "addEventListener('click'" in load.group(0)
    assert re.search(r"!state\.expandedJournalGroups\.has\(key\)", load.group(0))
    assert "classList.add('collapsed')" in load.group(0)
    toggle = re.search(r"function toggleJournalGroup\(.*?\n\}", app_js, re.DOTALL)
    assert toggle is not None
    assert "expandedJournalGroups.add" in toggle.group(0)
    assert "expandedJournalGroups.delete" in toggle.group(0)
    assert "classList.toggle('collapsed'" in toggle.group(0)
    css = (WEB / "style.css").read_text(encoding="utf-8")
    hidden = re.search(
        r"\.journal-group\.collapsed\s+\.journal-row\s*\{([^}]*)\}", css
    )
    assert hidden is not None
    assert "display: none" in hidden.group(1)


def test_toolbar_tab_survives_a_reload(app_js):
    """Connect / Sessions / Journals come back after a refresh; the transcript does not."""
    show = re.search(r"function showScreen\(.*?\n\}", app_js, re.DOTALL)
    assert show is not None
    assert "localStorage.setItem('osScreen'" in show.group(0)
    assert "localStorage.getItem('osScreen')" in app_js
    assert "restoreScreen" in app_js
    restore = re.search(r"function restoreScreen\(.*?\n\}", app_js, re.DOTALL)
    assert restore is not None
    body = restore.group(0)
    assert "connect" in body and "sessions" in body and "journals" in body
    assert "showScreen(saved)" in body
    tail = app_js.rsplit("connectRegistrySocket();", 1)[-1]
    assert "restoreScreen()" in tail
    assert "activeJournalId" not in restore.group(0)


def test_journal_list_shows_every_row_until_a_preview_opens(app_js, markup):
    """Full list on the empty form; six visible rows once a transcript is open."""
    assert "JOURNAL_LIST_LINES" not in app_js
    assert "journalListExpanded" not in app_js
    assert "journal-list-toggle" not in markup.classes
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert "list-expanded" not in css
    default = re.search(r"#journal-groups\s*\{([^}]*)\}", css)
    assert default is not None
    assert "6 *" not in default.group(1)
    capped = re.search(
        r"#screen-journals\.preview-open #journal-groups\s*\{([^}]*)\}", css
    )
    assert capped is not None
    assert "6 *" in capped.group(1)


def test_journal_list_hides_its_scrollbar():
    """Trackpad still scrolls; a visible bar sits on the row trash."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    groups = re.search(r"#journal-groups\s*\{([^}]*)\}", css)
    assert groups is not None
    assert "overflow: auto" in groups.group(1)
    assert "scrollbar-width: none" in groups.group(1)
    webkit = re.search(r"#journal-groups::-webkit-scrollbar\s*\{([^}]*)\}", css)
    assert webkit is not None
    assert "display: none" in webkit.group(1)


def test_cell_feed_hides_its_scrollbar():
    """Trackpad still scrolls; a visible bar sits on Collapse all."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    feed = re.search(r"\.feed\s*\{([^}]*)\}", css)
    assert feed is not None
    assert "overflow: auto" not in feed.group(1)
    cards = re.search(r"\.feed-cards\s*\{([^}]*)\}", css)
    assert cards is not None
    assert "overflow: auto" in cards.group(1)
    assert "scrollbar-width: none" in cards.group(1)
    assert "grid-auto-rows: min-content" in cards.group(1), (
        "a definite-height grid shrinks overflow:hidden cells; unfold then shows nothing"
    )
    webkit = re.search(r"\.feed-cards::-webkit-scrollbar\s*\{([^}]*)\}", css)
    assert webkit is not None
    assert "display: none" in webkit.group(1)


def test_closing_the_journal_preview_returns_to_the_list(app_js, markup):
    assert "journal-close" in markup.classes
    body = re.search(r"function closeJournalPreview\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    assert "activeJournalId = null" in body.group(0)
    assert "setJournalFocus(false)" in body.group(0)



def test_the_ui_watches_the_registry_for_sessions_it_did_not_open(app_js):
    """An agent's session must appear without the human reloading the page."""
    body = re.search(r"function connectRegistrySocket\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    assert "/ws/sessions`" in body.group(0)
    assert "session_opened" in body.group(0)
    assert "session_closed" in body.group(0)
    assert "loadSessionHistory" in body.group(0), "a new tab needs its feed filled in"
    assert "connectRegistrySocket()" in app_js


def test_attaching_the_same_session_twice_does_not_open_two_sockets(app_js):
    body = re.search(r"function attachSession\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    head = body.group(0).split("const record = newSessionRecord")[0]
    assert "state.sessions.get(info.id)" in head
    assert "return;" in head


def test_a_watcher_follows_commands_it_did_not_run(app_js):
    """Results reach the owner over HTTP; a watcher has to pull them itself."""
    handler = re.search(
        r"socket\.addEventListener\('message'.*?\n  \}\);", app_js, re.DOTALL
    )
    assert handler is not None
    assert "!keyFor(id)" in handler.group(0)
    assert "withLogs: false" in handler.group(0), "logs already stream over the socket"


def test_a_journal_row_offers_a_clipboard_copy(app_js, markup):
    body = re.search(r"async function copyJournal\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    assert "fmt=markdown" in body.group(0)
    assert "copyText" in body.group(0)
    assert "'copied'" in body.group(0), "a silent clipboard failure is worse than none"
    assert "journal-copy" in _classes_created_by_script(app_js) | markup.classes


def test_journal_row_export_controls_lift_cyan_without_a_ring():
    """copy/.jsonl/.md: no outline, cyan + 2px lift on hover."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    copy = re.search(r"\.journal-copy\s*\{([^}]*)\}", css)
    assert copy is not None
    assert "border: 0" in copy.group(1)
    hover = re.search(
        r"\.journal-export-links a:hover,\s*\.journal-copy:hover:not\(:disabled\)\s*\{([^}]*)\}",
        css,
    )
    assert hover is not None
    assert "var(--cyan)" in hover.group(1)
    assert "translateY(-2px)" in hover.group(1)
    assert "box-shadow" not in hover.group(1)


def test_clicking_a_row_control_does_not_also_open_the_preview(app_js):
    row = re.search(r"row\.addEventListener\('click'.*?\n  \}\);", app_js, re.DOTALL)
    assert row is not None
    assert "closest('a, button')" in row.group(0)


def test_journal_row_delete_is_immediate_and_skips_live_sessions(app_js):
    """Trash sits off the export links. A live session has no button. No confirm."""
    row = re.search(r"function journalRow\(.*?\n\}", app_js, re.DOTALL)
    assert row is not None
    assert "journal-delete" in row.group(0)
    assert "journalTrashButton" in row.group(0)
    assert "state.sessions.has" in row.group(0)
    assert "<svg" in app_js
    delete = re.search(r"async function deleteJournal\(.*?\n\}", app_js, re.DOTALL)
    assert delete is not None
    body = delete.group(0)
    assert "confirm(" not in body
    assert "api.del" in body
    assert "/api/journals/" in body
    assert "closeJournalPreview" in body
    assert "loadJournals" in body


def test_journal_group_delete_asks_before_removing_finished_files(app_js):
    """Group trash confirms; live journals in the group stay on disk."""
    load = re.search(r"async function loadJournals\(.*?\n\}", app_js, re.DOTALL)
    assert load is not None
    assert "journal-group-delete" in load.group(0)
    helper = re.search(r"function journalTrashButton\(.*?\n\}", app_js, re.DOTALL)
    assert helper is not None
    assert "stopPropagation" in helper.group(0)
    group = re.search(r"async function deleteJournalGroup\(.*?\n\}", app_js, re.DOTALL)
    assert group is not None
    body = group.group(0)
    assert "confirm(" in body
    assert "deleteJournal" in body
    assert "state.sessions.has" in body


def test_journal_delete_sits_apart_from_export_and_turns_red():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    row = re.search(
        r"\.journal-row,\s*\.journal-columns\s*\{([^}]*)\}", css
    )
    assert row is not None
    columns = re.findall(r"[\d.]+rem|auto", row.group(1).split("grid-template-columns")[-1])
    assert len(columns) >= 8
    trash = re.search(r"\.journal-delete\s*\{([^}]*)\}", css)
    assert trash is not None
    assert "margin-left" in trash.group(1)
    assert "border: 0" in trash.group(1)
    hover = re.search(
        r"\.journal-delete:hover:not\(:disabled\)\s*\{([^}]*)\}", css
    )
    assert hover is not None
    assert "var(--red)" in hover.group(1)


def test_download_all_is_gone(app_js):
    """One journal at a time; a bulk download nobody asked for is just risk."""
    assert "download-all" not in app_js


def test_the_session_header_does_not_offer_a_manual_resync(app_js, markup):
    """The registry socket already keeps tabs in sync; a second control is noise."""
    assert "session-refresh" not in markup.classes
    assert "async function refreshSession(" not in app_js


def test_closing_a_foreign_session_asks_for_the_admin_key(app_js):
    """A 403 must lead somewhere, not leave a tab that refuses to go away."""
    body = re.search(r"async function closeSession\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    assert "error.status !== 403" in body.group(0)
    assert "ensureAdminKey()" in body.group(0)


def test_the_admin_key_prompt_says_where_to_find_it(app_js):
    """A key you cannot locate is a dead end, and this one is never served."""
    body = re.search(r"async function ensureAdminKey\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    assert "~/.odoo-sheller/admin.key" in body.group(0)
    assert "cat ~/.odoo-sheller/admin.key" in body.group(0)


def test_owner_actions_only_ask_for_the_admin_key_when_refused(app_js):
    """Handing over your own session must not demand a second secret."""
    for name in ("handOver", "takeBack", "grantCommit"):
        body = re.search(rf"async function {name}\(.*?\n\}}", app_js, re.DOTALL)
        assert body is not None, name
        assert "withAdminRetry" in body.group(0), name
        head = body.group(0).split("try {")[0]
        assert "ensureAdminKey" not in head, f"{name} asks up front"


def test_a_handover_keeps_a_close_only_key(app_js):
    """You gave up typing, not the ability to stop what you started."""
    body = re.search(r"async function handOver\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    assert "rememberCloseKey" in body.group(0)
    assert "forgetKey(id)" in body.group(0), "it must not stay a write key"
    assert "JSON.stringify" in body.group(0)
    assert "session_id" in body.group(0)
    assert "write_key" in body.group(0)
    assert "copyText" in body.group(0)
    assert "accepted !== null" in body.group(0)

    closer = re.search(r"function closeKeyFor\(.*?\n\}", app_js, re.DOTALL)
    assert closer is not None
    assert "loadCloseKeys()" in closer.group(0)

    close = re.search(r"async function closeSession\(.*?\n\}", app_js, re.DOTALL)
    assert close is not None
    assert "closeKeyFor(id)" in close.group(0)


def test_a_handover_marker_is_not_labelled_a_transaction(app_js):
    body = re.search(r"function markerText\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    assert "owner_changed" in body.group(0)
    assert "Handed over" in body.group(0)
    assert "Commit granted" in body.group(0)


def test_a_rejected_admin_key_is_not_kept(app_js):
    """A wrong key in storage would make every later attempt fail identically."""
    body = re.search(r"async function withAdminRetry\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    assert "localStorage.removeItem('osAdminKey')" in body.group(0)


def test_granting_commit_uses_the_key_this_browser_holds(app_js):
    body = re.search(r"async function grantCommit\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    assert "closeKeyFor(id)" in body.group(0)


def test_session_header_does_not_export_the_journal(app_js):
    """Journals tab already exports; a second control on the live session is noise."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    template = html.split('id="session-panel"', 1)[1].split("</template>", 1)[0]
    assert "Export journal" not in template
    assert 'class="export"' not in template
    assert "Export journal" not in app_js


def test_session_keyboard_is_a_fixed_two_row_grid(markup, app_js):
    """Header actions are keys: always in the DOM, disabled rather than hidden."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    template = html.split('id="session-panel"', 1)[1].split("</template>", 1)[0]
    head = re.search(r'<header class="session-head">(.*?)</header>', template, re.DOTALL)
    assert head is not None
    block = head.group(1)
    identity = re.search(
        r'<div class="session-identity">(.*?)</div>\s*<div class="session-keys"',
        block,
        re.DOTALL,
    )
    assert identity is not None
    assert 'class="pending"' not in identity.group(1)
    assert "session-refresh" not in identity.group(1)
    assert 'class="badges"' in identity.group(1)
    assert identity.group(1).find("target") < identity.group(1).find("badges")
    assert 'class="meta-line"' in identity.group(1)
    assert identity.group(1).find("odoo") < identity.group(1).find("session-id")
    assert identity.group(1).find("session-id") < identity.group(1).find("session-age")
    assert 'class="session-opened"' in identity.group(1)
    assert 'class="meta-slash"' in identity.group(1)
    assert "grant-commit" not in block.split('class="session-keys"', 1)[0]
    assert 'class="session-keys"' in block
    assert 'class="actions"' not in block
    assert "Reconnect" not in template
    assert 'class="reconnect"' not in template
    assert 'class="run' not in template
    assert 'type="checkbox"' not in block
    assert "Revoke commit" not in app_js
    keys = [
        "grant-commit",
        "grant-access",
        "close",
        "kill",
        "interrupt",
        "rollback",
        "commit",
        "new",
    ]
    positions = [block.find(f"session-key {name}") for name in keys]
    assert all(pos >= 0 for pos in positions), keys
    assert positions == sorted(positions)
    assert "session-key enter" not in block
    assert "session-key handover" not in block
    assert "session-key takeback" not in block
    for name in keys:
        match = re.search(rf'<button[^>]*class="session-key {name}"[^>]*>', block)
        assert match is not None, name
        assert "hidden" not in match.group(0), name
        assert "title=" in match.group(0), name
    assert ">Grant commit<" in block
    assert ">Grant access<" in block
    assert ">Interrupt<" in block
    assert ">Kill<" in block
    assert ">New<" in block
    assert "session-keys" in markup.classes
    assert "session-key" in markup.classes
    assert "reconnectSession" not in app_js
    for selector in (
        ".interrupt').hidden",
        ".commit').hidden",
        ".rollback').hidden",
        "grant.hidden",
    ):
        assert selector not in app_js, selector
    assert "enter.disabled" not in app_js
    assert "grant.disabled" in app_js
    assert "querySelector('.handover')" not in app_js
    assert "querySelector('.takeback')" not in app_js
    assert "handOver(id)" in app_js
    assert "takeBack(id)" in app_js
    assert "interrupt.disabled" in app_js
    assert "querySelector('.new')" in app_js
    assert "duplicateSession(id)" in app_js
    assert "if (state.activeSession === info.id && state.sessions.has(id))" in app_js
    assert "record.duplicating" in app_js
    assert "aria-pressed" in app_js
    assert "runCommand(id)" in app_js
    assert "querySelector('.enter')" not in app_js
    grant = re.search(r"async function grantCommit\(.*?\n\}", app_js, re.DOTALL)
    assert grant is not None
    assert "confirm(" in grant.group(0)
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert ".grant-commit .switch" not in css
    assert ".grant-commit:has(input:checked)" not in css
    keys_rule = re.search(r"\.session-keys\s*\{([^}]*)\}", css)
    assert keys_rule is not None
    assert "display: grid" in keys_rule.group(1)
    assert "repeat(4" in keys_rule.group(1)
    assert "1.55rem" in keys_rule.group(1)
    assert "gap: 3px" in keys_rule.group(1)
    assert "width: calc(4 * 6.2rem)" in keys_rule.group(1)
    assert "height: calc(2 * 1.55rem)" in keys_rule.group(1)
    assert "margin: 0 0 0 auto" in keys_rule.group(1)
    editor = re.search(r"\.editor-pane\s*\{([^}]*)\}", css)
    assert editor is not None
    assert "16px" not in editor.group(1)
    assert "grid-column:" not in css
    head_rule = [
        match.group(1)
        for match in re.finditer(r"\.session-head\s*\{([^}]*)\}", css)
        if "padding: 0" in match.group(1)
    ]
    assert head_rule, "session-head must have no padding so the pad can tile it"
    assert "overflow: visible" in head_rule[0]
    assert "border-bottom" not in head_rule[0]
    assert "var(--session-stack)" in head_rule[0]
    assert "--session-stack: 10px" in css
    assert "var(--session-stack)" in editor.group(1)
    key_rule = re.search(r"\.session-key\s*\{([^}]*)\}", css)
    assert key_rule is not None
    assert "var(--cyan" in key_rule.group(1)
    assert "var(--panel-raised)" in key_rule.group(1)
    assert "nowrap" in key_rule.group(1)
    assert "border-radius: 4px" in key_rule.group(1)
    close_label = re.search(r"\.session-key\.close:not\(:disabled\)\s*\{([^}]*)\}", css)
    kill_label = re.search(r"\.session-key\.kill:not\(:disabled\)\s*\{([^}]*)\}", css)
    new_label = re.search(r"\.session-key\.new:not\(:disabled\)\s*\{([^}]*)\}", css)
    assert close_label is not None
    assert "var(--amber)" in close_label.group(1)
    assert kill_label is not None
    assert "var(--red)" in kill_label.group(1)
    assert new_label is not None
    assert "oklch(1 0 0)" in new_label.group(1)
    assert ".session-key.grant-commit[aria-pressed=\"true\"]" in css
    assert ".session-key.grant-access[aria-pressed=\"true\"]" in css
    latch = re.search(
        r"\.session-key\.grant-commit\[aria-pressed=\"true\"\],\s*"
        r"\.session-key\.grant-access\[aria-pressed=\"true\"\]\s*\{([^}]*)\}",
        css,
    )
    assert latch is not None
    assert "var(--amber" in latch.group(1)
    assert "translateY" in re.search(
        r"button:active:not\(:disabled\)\s*\{([^}]*)\}", css
    ).group(1)
    assert re.search(r"\.run\s*\{", css) is None


def test_new_key_spins_while_a_session_is_opening(app_js):
    """Registry load takes seconds; New must show it is working, not just go inert."""
    assert "classList.toggle('busy', !!record.duplicating)" in app_js
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert re.search(r"@keyframes\s+spin\s*\{", css)
    mark = re.search(r"\.session-key\.new\.busy::after\s*\{([^}]*)\}", css)
    assert mark is not None
    assert "animation:" in mark.group(1)
    assert "↻" in mark.group(1)
    assert "var(--amber)" in mark.group(1)


def test_start_button_spins_while_a_session_is_opening(app_js):
    """Same spinning arrow as New; the card still says the registry is loading."""
    assert "Loading Odoo registry" in app_js
    render = re.search(r"function renderContainers\(.*?\n\}", app_js, re.DOTALL)
    assert render is not None
    assert "busy" in render.group(0)
    assert "aria-busy" in render.group(0)
    css = (WEB / "style.css").read_text(encoding="utf-8")
    mark = re.search(r"\.start\.busy::after\s*\{([^}]*)\}", css)
    assert mark is not None
    assert "animation:" in mark.group(1)
    assert "↻" in mark.group(1)
    assert "var(--amber)" in mark.group(1)


def test_connect_card_streams_stderr_while_the_registry_loads(app_js, markup):
    """POST waits for hello; the card must show Odoo's log in the meantime."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    template = html.split('id="container-card"', 1)[1].split("</template>", 1)[0]
    assert 'class="startup-log"' in template
    assert template.find('class="picker"') < template.find("startup-log")
    assert "startup-log" in markup.classes

    css = (WEB / "style.css").read_text(encoding="utf-8")
    well = re.search(r"\.startup-log\s*\{([^}]*)\}", css)
    assert well is not None
    assert "min-height" in well.group(1)
    assert "max-height" in well.group(1)
    assert "12" in well.group(1)
    assert "overflow: auto" in well.group(1)
    assert "var(--background)" in well.group(1)

    assert "startups:" in app_js
    render = re.search(r"function renderContainers\(.*?\n\}", app_js, re.DOTALL)
    assert render is not None
    assert "startup-log" in render.group(0)
    assert "startups.get" in render.group(0)

    start = re.search(r"async function startSession\(.*?\n\}", app_js, re.DOTALL)
    assert start is not None
    assert "startups.set" in start.group(0)
    assert "stopStartup" in start.group(0)
    assert "logLines" in start.group(0)

    assert "connectStartupSocket" in app_js
    assert "/logs?tail=" in app_js
    assert "mergeLogLines" in app_js

    registry = re.search(r"function connectRegistrySocket\(.*?\n\}", app_js, re.DOTALL)
    assert registry is not None
    body = registry.group(0)
    assert "session_starting" in body
    _, _, rest = body.partition("session_starting")
    starting_block = rest.split("session_opened", 1)[0]
    assert "attachSession" not in starting_block
    opened_block = rest.split("session_opened", 1)[1].split("session_closed", 1)[0]
    assert "attachSession" in opened_block
    assert "startupOwns" in opened_block


def test_a_failed_start_keeps_the_startup_log_on_the_card(app_js):
    """The well is why the start failed; clearing it with the spinner would hide that."""
    start = re.search(r"async function startSession\(.*?\n\}", app_js, re.DOTALL)
    assert start is not None
    catch = start.group(0).split("catch", 1)[1]
    assert "failed" in catch
    assert "stopStartup" not in catch
    assert "startups.delete" not in catch


def test_the_startup_card_survives_a_refresh_mid_start(app_js):
    """loadContainers renders once with probe: null; the well must not take the list down."""
    fill = re.search(r"function fillPicker\(.*?\n\}", app_js, re.DOTALL)
    assert fill is not None
    assert "container.probe.databases" not in fill.group(0), (
        "a Refresh blanks the probe before the re-probe lands"
    )
    assert "container.probe?." in fill.group(0)
    render = re.search(r"function renderContainers\(.*?\n\}", app_js, re.DOTALL)
    assert render is not None
    branch = render.group(0).split("state.startups.get(container.name)", 1)[1]
    guarded = re.search(r"if \(container\.probe\) \{\s*\n\s*fillPicker", branch)
    assert guarded is not None, "the picker needs a probe; the log well does not"
    assert "startup-log" in branch, "the well still shows while the probe is away"


def test_the_startup_well_does_not_undo_a_scroll_back(app_js):
    """The card is rebuilt every render, so the scroll intent lives on the record."""
    start = re.search(r"async function startSession\(.*?\n\}", app_js, re.DOTALL)
    assert start is not None
    assert "pinned: true" in start.group(0)
    render = re.search(r"function renderContainers\(.*?\n\}", app_js, re.DOTALL)
    assert render is not None
    branch = render.group(0).split("state.startups.get(container.name)", 1)[1]
    assert "'scroll'" in branch, "nothing else can notice the reader scrolled"
    # scrollTop on a card still inside the detached fragment is a silent no-op,
    # so the restore has to come after the card is in the document.
    assert branch.index("list.append(fragment)") < branch.index("restoreStartupScroll")
    restore = re.search(r"function restoreStartupScroll\(.*?\n\}", app_js, re.DOTALL)
    assert restore is not None
    assert "startup.pinned" in restore.group(0), "an unpinned well must keep its offset"
    assert "startup.scrollTop" in restore.group(0)
    detached = branch.split("list.append(fragment)")[0]
    assert re.search(r"well\.scrollTop\s*=", detached) is None, (
        "no scrolling while the card is detached"
    )


def test_startup_log_follows_live_stderr_instead_of_dumping_the_tail(app_js):
    """The HTTP tail and live WS both feed one paced queue, not a single replaceChildren dump."""
    connect = re.search(r"function connectStartupSocket\(.*?\n\}", app_js, re.DOTALL)
    assert connect is not None
    text = connect.group(0)
    assert "enqueueStartupLine" in text
    live, _, rest = text.partition("api.get")
    assert "hydrated" in live
    assert "enqueueStartupLine" in rest, "GET /logs must seed the well through the paced queue"
    assert "paintStartupLog" not in text
    assert "function enqueueStartupLine" in app_js
    assert "requestAnimationFrame" in app_js
    drain = re.search(r"function drainStartupLog\(.*?\n\}", app_js, re.DOTALL)
    assert drain is not None
    assert "appendStartupLine" in drain.group(0)
    render = re.search(r"function renderContainers\(.*?\n\}", app_js, re.DOTALL)
    assert "startup.view" in render.group(0)
    start = re.search(r"async function startSession\(.*?\n\}", app_js, re.DOTALL)
    assert "view: []" in start.group(0)
    assert "pending: []" in start.group(0)
    merge = re.search(r"function mergeLogLines\(.*?\n\}", app_js, re.DOTALL)
    assert merge is not None
    assert "slice" in merge.group(0)


def test_a_watcher_stops_on_session_failed(app_js):
    """Only the caller of POST sees the exception; a watcher needs the event."""
    handler = re.search(
        r"function connectRegistrySocket\(.*?\n\}\n", app_js, re.DOTALL
    )
    assert handler is not None
    assert "session_failed" in handler.group(0)
    body = re.search(r"function failStartupById\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    assert "failed = true" in body.group(0)
    assert "socket" in body.group(0), "a dead session's stream must be let go"
    assert "renderContainers()" in body.group(0)


def test_the_startup_claims_only_its_own_session(app_js):
    """An agent opening the same container and database must not be adopted."""
    start = re.search(r"async function startSession\(.*?\n\}", app_js, re.DOTALL)
    assert start is not None
    assert "client_token" in start.group(0), "the POST has to carry the token"
    assert "token" in start.group(0)
    watch = re.search(r"function watchStartupFromEvent\(.*?\n\}", app_js, re.DOTALL)
    assert watch is not None
    assert "client_token" in watch.group(0)
    assert "info.database" not in watch.group(0), (
        "(container, database) is not an identity"
    )
    adopt = re.search(r"async function adoptStartingSession\(.*?\n\}", app_js, re.DOTALL)
    assert adopt is not None
    assert "client_token" in adopt.group(0)


def test_startup_log_adopts_a_starting_session_from_the_listing(app_js):
    """session_starting can be missed; GET /api/sessions already lists a session in starting."""
    body = re.search(r"async function adoptStartingSession\(.*?\n\}", app_js, re.DOTALL)
    assert body is not None
    assert "api.get('/api/sessions')" in body.group(0)
    assert "state === 'starting'" in body.group(0)
    start = re.search(r"async function startSession\(.*?\n\}", app_js, re.DOTALL)
    assert start is not None
    assert "adoptStartingSession" in start.group(0)
