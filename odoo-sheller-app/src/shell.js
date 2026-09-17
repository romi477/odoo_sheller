const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const MIN_DOCK = 120;
const DEFAULT_DOCK = 280;
const DOCK_STEP = 80;

const frame = document.getElementById("ui");
const splash = document.getElementById("splash");
const status = document.getElementById("status");
const actions = document.getElementById("actions");
const dock = document.getElementById("dock");
const grip = document.getElementById("grip");
const tabsEl = document.getElementById("tabs");
const panesEl = document.getElementById("panes");
const newTabBtn = document.getElementById("new-tab");
const toggleBtn = document.getElementById("dock-toggle");
const mcp = document.getElementById("mcp");

// Not `window.alert`: WKWebView draws no JavaScript dialog unless the host
// implements the WKUIDelegate panels, and wry implements only the file-upload
// and media-permission ones. The plugin dialog is native and actually appears.
async function fail(message) {
  try {
    await window.__TAURI__.dialog.message(String(message), {
      title: "odoo-sheller",
      kind: "error",
    });
  } catch (err) {
    console.error("shell:", message, err);
  }
}

/* ---------------------------------------------------------------- startup */

// The port is fixed, and fixed in one place: `daemon.rs`. Asking keeps this
// page from becoming a second answer to the same question.
let uiUrl = null;

function render(state) {
  if (state.phase === "error") {
    status.textContent = state.message;
    status.classList.add("error");
    actions.hidden = false;
    splash.hidden = false;
    frame.hidden = true;

    return;
  }
  if (state.phase === "ready") {
    // Assigning the same src again would not reload it; that is what the
    // Reload menu item is for.
    invoke("ui_url").then((url) => {
      uiUrl = url;
      if (frame.getAttribute("src") !== url) {
        frame.setAttribute("src", url);
      }
      frame.hidden = false;
      splash.hidden = true;
    });

    return;
  }
  status.textContent = "Starting…";
  status.classList.remove("error");
  actions.hidden = true;
  splash.hidden = false;
  frame.hidden = true;
}

document.getElementById("retry").addEventListener("click", () => {
  render({ phase: "starting" });
  invoke("retry_startup");
});
document.getElementById("quit").addEventListener("click", () => invoke("quit_app"));

listen("startup", (event) => render(event.payload));
listen("reload-ui", () => {
  // The frame, not this page: reloading the shell would drop every terminal.
  // `src` again rather than `contentWindow.location.reload()`, which a
  // cross-origin frame does not allow.
  if (uiUrl) {
    frame.setAttribute("src", uiUrl);
  }
});
invoke("startup_state").then(render);

/* ----------------------------------------------------------- mcp config */

// Rust decides what the entry is and puts the first form on the clipboard;
// this only draws it. The prose lives in `index.html` beside the markup it
// belongs to.
function showMcpConfig(config) {
  document.getElementById("mcp-servers").textContent = config.mcp_servers;
  document.getElementById("context-servers").textContent = config.context_servers;
  document.getElementById("mcp-linked").hidden = !config.linked;

  const list = document.getElementById("mcp-locations");
  list.textContent = "";
  for (const { app, path } of config.locations) {
    const term = document.createElement("dt");
    term.textContent = app;
    const value = document.createElement("dd");
    value.textContent = path;
    list.append(term, value);
  }

  const clipboard = document.getElementById("mcp-clipboard");
  clipboard.classList.toggle("error", Boolean(config.clipboard_error));
  clipboard.textContent = config.clipboard_error
    ? `Clipboard unavailable: ${config.clipboard_error}`
    : "The mcpServers form is on the clipboard.";

  // Asking for the dialog while it is already up is a no-op, not an error:
  // `showModal` on an open dialog throws.
  if (!mcp.open) {
    mcp.showModal();
  }
  mcp.scrollTop = 0;
}

// The button says what happened, because nothing else can: a copy leaves no
// trace on screen. The label and the timer live beside the button rather than
// on it — `dataset` is strings, and a timer id is not one.
const copyState = new WeakMap();

async function copy(button, text) {
  const state = copyState.get(button) || { label: button.textContent };
  clearTimeout(state.timer);
  try {
    await invoke("copy_text", { text });
    button.textContent = "Copied";
    button.classList.add("done");
  } catch (err) {
    button.textContent = "Failed";
    console.error("copy:", err);
  }
  state.timer = setTimeout(() => {
    button.textContent = state.label;
    button.classList.remove("done");
  }, 1400);
  copyState.set(button, state);
}

for (const button of mcp.querySelectorAll(".copy")) {
  button.addEventListener("click", () =>
    copy(button, document.getElementById(button.dataset.copy).textContent),
  );
}
document.getElementById("mcp-done").addEventListener("click", () => mcp.close());
listen("mcp-config", (event) => showMcpConfig(event.payload));

/* --------------------------------------------------------------- terminal */

const tabs = [];
const closing = new Set();
let activeId = null;

// `--terminal` in `styles.css` is the same black; xterm paints its own
// canvas and cannot read a CSS variable.
const THEME = {
  background: "#000000",
  foreground: "#ddd8ea",
  cursor: "#5ec8d8",
  selectionBackground: "#3a3560",
};

function decodePayload(payload) {
  const binary = atob(payload);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }

  return bytes;
}

function dockOpen() {
  return !dock.classList.contains("closed");
}

// The height to come back to. Hiding the dock is not closing the terminals —
// they keep running — so reopening returns them to the size they were left at.
// Closing the last tab is the other thing, and resets this: the next terminal
// is a new one and starts at the usual height.
let dockHeight = DEFAULT_DOCK;

function setDock(open) {
  if (!open && dockOpen()) {
    dockHeight = tabs.length ? dock.getBoundingClientRect().height : DEFAULT_DOCK;
  }
  dock.classList.toggle("closed", !open);
  toggleBtn.classList.toggle("on", open);
  if (open) {
    // Clamped on the way back in as well as on the way out: the window may
    // have been made shorter while the dock was hidden.
    dock.style.height = `${clampDock(dockHeight)}px`;
    const tab = tabs.find((item) => item.id === activeId);
    if (tab) {
      fitAndResize(tab);
      tab.term.focus();
    }
  } else {
    dock.style.removeProperty("height");
  }
}

/// The height the dock may take: never taller than the window less a strip of
/// the UI above it, never shorter than a usable terminal. The drag and the two
/// shortcuts clamp the same way, so neither can reach a size the other cannot.
function clampDock(height) {
  return Math.min(Math.max(MIN_DOCK, height), window.innerHeight - 120);
}

function resizeDock(delta) {
  if (!dockOpen()) {
    // Taller with the dock shut means "show it"; shorter means nothing.
    if (delta > 0) {
      toggleDock();
    }

    return;
  }
  dock.style.height = `${clampDock(dock.getBoundingClientRect().height + delta)}px`;
  const tab = tabs.find((item) => item.id === activeId);
  if (tab) {
    fitAndResize(tab);
  }
}

// xterm fits whole rows into a height it measures in CSS pixels, and a row is
// rarely a whole number of them: the last one can end a few pixels below the
// box, where the frame clips it in half. Drop that row instead of showing part
// of it — a terminal with a sliced bottom line is what a frame is there to
// prevent.
function trimClippedRow(tab) {
  const screen = tab.pane.querySelector(".xterm-screen");
  if (!screen || tab.term.rows < 2) {
    return;
  }
  const pane = tab.pane.getBoundingClientRect();
  const floor = pane.bottom - parseFloat(getComputedStyle(tab.pane).paddingBottom);
  if (screen.getBoundingClientRect().bottom > floor + 0.5) {
    tab.term.resize(tab.term.cols, tab.term.rows - 1);
  }
}

function fitAndResize(tab) {
  if (!dockOpen()) {
    return;
  }
  tab.fit.fit();
  trimClippedRow(tab);
  invoke("pty_resize", { id: tab.id, cols: tab.term.cols, rows: tab.term.rows });
}

// The shell's own name, asked for once: `zsh`, `bash`, whatever `$SHELL` is
// on this machine. `~` because that is where the shell is started.
let shellName = "sh";
invoke("shell_name").then((name) => {
  shellName = name;
  renumberTabs();
});

// One tab needs no number; more than one do, and they are numbered by where
// they sit rather than by when they were opened — close the first of three
// and the two left are 1 and 2, not 2 and 3.
function renumberTabs() {
  tabs.forEach((tab, index) => {
    tab.button.querySelector(".title").textContent =
      tabs.length > 1 ? `${shellName} ~ ${index + 1}` : `${shellName} ~`;
  });
}

function setActive(id) {
  activeId = id;
  for (const tab of tabs) {
    tab.button.classList.toggle("active", tab.id === id);
    tab.pane.classList.toggle("active", tab.id === id);
  }
  const tab = tabs.find((item) => item.id === id);
  if (tab) {
    fitAndResize(tab);
    tab.term.focus();
  }
}

async function closeTab(id) {
  // A tab can be closed twice at once: the × and the shell exiting. The guard
  // is dropped again on the way out of every branch, so a close that found
  // nothing does not make the id unclosable forever.
  if (closing.has(id)) {
    return;
  }
  closing.add(id);
  const index = tabs.findIndex((item) => item.id === id);
  if (index < 0) {
    closing.delete(id);

    return;
  }
  const [tab] = tabs.splice(index, 1);
  renumberTabs();
  tab.unlistenOutput();
  tab.unlistenExit();
  tab.term.dispose();
  tab.button.remove();
  tab.pane.remove();
  await invoke("pty_close", { id });
  closing.delete(id);
  if (tabs.length === 0) {
    activeId = null;
    setDock(false);

    return;
  }
  if (activeId === id) {
    setActive(tabs[Math.max(0, index - 1)].id);
  }
}

async function openTab() {
  let id;
  try {
    id = await invoke("pty_create");
  } catch (err) {
    await fail(err);

    return;
  }
  const button = document.createElement("button");
  button.type = "button";
  button.className = "tab";
  button.innerHTML = `<span class="title"></span><span class="close" title="Close">×</span>`;
  button.addEventListener("click", (event) => {
    if (event.target.closest(".close")) {
      closeTab(id);

      return;
    }
    setActive(id);
  });
  tabsEl.appendChild(button);

  const pane = document.createElement("div");
  pane.className = "pane";
  panesEl.appendChild(pane);

  const term = new Terminal({
    cursorBlink: true,
    fontSize: 13,
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, monospace",
    theme: THEME,
    allowProposedApi: false,
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(pane);
  term.onData((data) => invoke("pty_write", { id, data }));

  const unlistenOutput = await listen(`pty-output-${id}`, (event) => {
    term.write(decodePayload(event.payload));
  });
  const unlistenExit = await listen(`pty-exit-${id}`, () => closeTab(id));

  tabs.push({ id, term, fit, button, pane, unlistenOutput, unlistenExit });
  renumberTabs();
  setDock(true);
  setActive(id);
}

function newTab() {
  if (!dockOpen() && tabs.length) {
    setDock(true);

    return;
  }
  openTab();
}

function toggleDock() {
  if (dockOpen()) {
    setDock(false);

    return;
  }
  if (tabs.length === 0) {
    openTab();

    return;
  }
  setDock(true);
}

// Wrapping, because the tabs are a ring: the step past the last one is the
// first, which is what every terminal with tabs does. Nothing to do with the
// dock shut — the keys move between tabs, they do not conjure one.
function cycleTab(step) {
  if (!dockOpen() || tabs.length < 2) {
    return;
  }
  const index = tabs.findIndex((item) => item.id === activeId);
  if (index < 0) {
    return;
  }
  setActive(tabs[(index + step + tabs.length) % tabs.length].id);
}

newTabBtn.addEventListener("click", () => openTab());
toggleBtn.addEventListener("click", toggleDock);
listen("terminal-toggle", toggleDock);
listen("terminal-new-tab", newTab);
// The screens live in the framed UI, which is a remote origin: no Tauri IPC
// crosses into it and no stylesheet does either, so the only way in is a
// message. It carries a step and nothing else.
function moveScreen(step) {
  if (!uiUrl || !frame.contentWindow) {
    return;
  }
  frame.contentWindow.postMessage({ type: "os-screen", step }, new URL(uiUrl).origin);
}

listen("screen-prev", () => moveScreen(-1));
listen("screen-next", () => moveScreen(1));
listen("terminal-prev", () => cycleTab(-1));
listen("terminal-next", () => cycleTab(1));
listen("terminal-taller", () => resizeDock(DOCK_STEP));
listen("terminal-shorter", () => resizeDock(-DOCK_STEP));

/* ------------------------------------------------------------------ grip */

grip.addEventListener("mousedown", (event) => {
  if (!dockOpen()) {
    return;
  }
  event.preventDefault();
  const startY = event.clientY;
  const startHeight = dock.getBoundingClientRect().height;
  // The frame swallows mouse events while the pointer is over it, which is
  // most of the drag.
  frame.style.pointerEvents = "none";
  const onMove = (move) => {
    dock.style.height = `${clampDock(startHeight + (startY - move.clientY))}px`;
  };
  const onUp = () => {
    window.removeEventListener("mousemove", onMove);
    window.removeEventListener("mouseup", onUp);
    frame.style.removeProperty("pointer-events");
    const tab = tabs.find((item) => item.id === activeId);
    if (tab) {
      fitAndResize(tab);
    }
  };
  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", onUp);
});

window.addEventListener("resize", () => {
  const tab = tabs.find((item) => item.id === activeId);
  if (tab) {
    fitAndResize(tab);
  }
});

// Cmd+T and the arrows are the menu's. Cmd+W is not, and stays here: as a menu
// item it would be taken from the window even with no terminal open, and the
// framed UI binds the same key for closing a session — two documents, one key,
// and whichever has focus answers.
window.addEventListener("keydown", (event) => {
  if (event.metaKey && event.key === "w" && dockOpen() && activeId) {
    event.preventDefault();
    closeTab(activeId);
  }
});
