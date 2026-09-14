const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const MIN_DOCK = 120;
const DEFAULT_DOCK = 280;

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

/* --------------------------------------------------------------- terminal */

const tabs = [];
const closing = new Set();
let activeId = null;

const THEME = {
  background: "#04030e",
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

function setDock(open) {
  dock.classList.toggle("closed", !open);
  toggleBtn.classList.toggle("on", open);
  if (open) {
    if (!dock.style.height) {
      dock.style.height = `${DEFAULT_DOCK}px`;
    }
    const tab = tabs.find((item) => item.id === activeId);
    if (tab) {
      fitAndResize(tab);
      tab.term.focus();
    }
  } else {
    dock.style.removeProperty("height");
  }
}

function fitAndResize(tab) {
  if (!dockOpen()) {
    return;
  }
  tab.fit.fit();
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

newTabBtn.addEventListener("click", () => openTab());
toggleBtn.addEventListener("click", toggleDock);
listen("terminal-toggle", toggleDock);
listen("terminal-new-tab", newTab);

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
    const height = Math.min(
      Math.max(MIN_DOCK, startHeight + (startY - move.clientY)),
      window.innerHeight - 120,
    );
    dock.style.height = `${height}px`;
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

// Cmd+T and Ctrl+` are the menu's. Cmd+W has no menu item of its own, and
// closing a tab is not closing the window.
window.addEventListener("keydown", (event) => {
  if (event.metaKey && event.key === "w" && dockOpen() && activeId) {
    event.preventDefault();
    closeTab(activeId);
  }
});
