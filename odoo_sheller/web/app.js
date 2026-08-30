const JOURNAL_UNMASKED_WARNING =
  'Journals are unmasked and may contain credentials read from the database.';
const DATABASE_LIST_FALLBACK =
  'could not read database list — enter the name manually';
const DEAD_CAUSE_TAIL = 20;
const LOG_PIN_SLACK = 24;
const state = {
  screen: 'connect',
  containers: [],
  sessions: new Map(),
  activeSession: null,
  offline: false,
  cellCounter: 0,
  activeJournalId: null,
  journalFocused: false,
  journals: [],
  expandedJournalGroups: new Set(),
  startups: new Map(),
};

const TESTING_HOLD_MS = 1200;

function sessionIsTesting(record) {
  return record.info.activity === 'run_test'
    || Date.now() < (record.testingUntil || 0);
}

function noteTesting(record, activity) {
  if (activity !== 'run_test') {

    return;
  }
  record.testingUntil = Date.now() + TESTING_HOLD_MS;
  if (record.testingTimer) {
    clearTimeout(record.testingTimer);
  }
  record.testingTimer = setTimeout(() => {
    record.testingTimer = null;
    renderSessions();
  }, TESTING_HOLD_MS + 20);
}

function dropTestingTimer(record) {
  if (record?.testingTimer) {
    clearTimeout(record.testingTimer);
    record.testingTimer = null;
  }
}

function newSessionRecord(info) {
  return {
    info,
    socket: null,
    cells: [],
    logLines: [],
    unseenLogs: 0,
    editor: null,
    history: [],
    historyIndex: 0,
    reattached: false,
    socketOffline: false,
    panel: null,
    editorTall: false,
    logsFocused: false,
    nextOrdinal: 1,
    closing: false,
    closingForce: false,
    duplicating: false,
    hydrated: false,
    awaitingInFlight: false,
    openedAt: null,
    testingUntil: 0,
    testingTimer: null,
  };
}

// Write keys are what make ownership real: without one, this page physically
// cannot type into a session, whether or not a button is on screen.
function loadKeys() {
  try {

    return JSON.parse(localStorage.getItem('osKeys') || '{}');
  } catch (_error) {

    return {};
  }
}

function saveKey(id, key) {
  const keys = loadKeys();
  keys[id] = key;
  localStorage.setItem('osKeys', JSON.stringify(keys));
}

function forgetKey(id) {
  const keys = loadKeys();
  delete keys[id];
  localStorage.setItem('osKeys', JSON.stringify(keys));
}

function keyFor(id) {

  return loadKeys()[id] || '';
}

// Kept after a handover: not enough to type into the session, enough to close
// what this browser started.
function loadCloseKeys() {
  try {

    return JSON.parse(localStorage.getItem('osCloseKeys') || '{}');
  } catch (_error) {

    return {};
  }
}

function rememberCloseKey(id, key) {
  const keys = loadCloseKeys();
  keys[id] = key;
  localStorage.setItem('osCloseKeys', JSON.stringify(keys));
}

function forgetCloseKey(id) {
  const keys = loadCloseKeys();
  delete keys[id];
  localStorage.setItem('osCloseKeys', JSON.stringify(keys));
}

function closeKeyFor(id) {

  return keyFor(id) || loadCloseKeys()[id] || '';
}

function adminKey() {

  return localStorage.getItem('osAdminKey') || '';
}

function authHeaders(id, {admin = false} = {}) {
  const headers = {};
  const key = id ? keyFor(id) : '';
  if (key) {
    headers['X-OS-Session-Key'] = key;
  }
  if (admin && adminKey()) {
    headers['X-OS-Admin-Key'] = adminKey();
  }

  return headers;
}

const api = {
  async get(path) {
    const response = await fetch(path);

    return check(response);
  },
  async post(path, body, headers = {}) {
    const response = await fetch(path, {
      method: 'POST',
      headers: {'content-type': 'application/json', ...headers},
      body: body === undefined ? null : JSON.stringify(body),
    });

    return check(response);
  },
  async del(path, headers = {}) {

    return check(await fetch(path, {method: 'DELETE', headers}));
  },
};

async function check(response) {
  if (response.ok) {

    return response.json();
  }
  const detail = (await response.json().catch(() => ({}))).detail || response.statusText;
  const message = typeof detail === 'string'
    ? detail
    : (detail.message || detail.recovery || detail.error || response.statusText);
  throw Object.assign(new Error(message), {status: response.status, detail});
}

function escapeHtml(value) {
  const element = document.createElement('div');
  element.textContent = value == null ? '' : String(value);

  return element.innerHTML;
}

function confirmJournalExport(event) {
  if (!confirm(`${JOURNAL_UNMASKED_WARNING}\n\nContinue with export?`)) {
    event.preventDefault();
  }
}

function bindJournalExportLink(link, label) {
  link.title = JOURNAL_UNMASKED_WARNING;
  link.setAttribute('aria-label', `${label} — ${JOURNAL_UNMASKED_WARNING}`);
  link.addEventListener('click', confirmJournalExport);
}

function setOffline(offline) {
  state.offline = offline;
  document.querySelector('#offline').hidden = !offline;
}

async function request(operation) {
  try {
    const value = await operation();
    setOffline(false);

    return value;
  } catch (error) {
    if (!error.status) {
      setOffline(true);
    }
    throw error;
  }
}

function showScreen(screen) {
  state.screen = screen;
  localStorage.setItem('osScreen', screen);
  document.querySelectorAll('.screen').forEach((element) => {
    element.hidden = element.id !== `screen-${screen}`;
  });
  document.querySelectorAll('#top [data-screen]').forEach((button) => {
    button.classList.toggle('active', button.dataset.screen === screen);
  });
  if (screen === 'journals') {
    loadJournals();
  }
  if (screen === 'sessions') {
    renderSessions();
  }
}

function sessionForContainer(container) {

  return [...state.sessions.values()].find((record) => record.info.container === container);
}

function renderContainers() {
  const list = document.querySelector('#containers');
  list.replaceChildren();
  state.containers.forEach((container) => {
    const fragment = document.querySelector('#container-card').content.cloneNode(true);
    const card = fragment.querySelector('.card');
    card.dataset.container = container.name;
    card.querySelector('.name').textContent = container.name;
    card.querySelector('.meta').textContent = `${container.image || 'unknown image'} · ${container.status || ''}`;
    card.querySelector('.open').disabled = !container.probe?.ok || !container.probe?.supported;
    card.querySelector('.open').addEventListener('click', () => openPicker(container.name));
    card.querySelector('.reprobe').addEventListener('click', () => probeContainer(container.name));
    card.querySelector('.start').addEventListener('click', () => startSession(container.name));
    card.querySelector('.close-connected').addEventListener('click', () => {
      const record = sessionForContainer(container.name);
      if (record) {
        closeSession(record.info.id, false);
      }
    });
    card.addEventListener('click', (event) => {
      if (event.target.closest('.picker') || event.target.closest('.open')) {

        return;
      }
      const opening = state.startups.get(container.name);
      if (opening && !opening.failed) {

        return;
      }
      closeAllPickers();
    });

    const note = card.querySelector('.probe-note');
    note.classList.remove('error');
    if (container.probing || !container.probe) {
      note.textContent = 'probing container…';
    } else if (!container.probe.ok || !container.probe.supported) {
      note.textContent = container.probe.error || 'Probe failed';
      note.classList.add('error');
    } else if (container.probe.error) {
      note.textContent = container.probe.error || DATABASE_LIST_FALLBACK;
      note.classList.add('error');
      card.querySelector('.meta').textContent =
        `${container.image || 'unknown image'} · ${container.status || ''} · Odoo ${container.probe.odoo_version}`;
    } else {
      const config = container.probe.config ? ` · ${container.probe.config}` : '';
      note.textContent = `Odoo ${container.probe.odoo_version} · Python ${container.probe.python}${config}`;
      card.querySelector('.meta').textContent =
        `${container.image || 'unknown image'} · ${container.status || ''} · Odoo ${container.probe.odoo_version}`;
    }

    const connected = sessionForContainer(container.name);
    if (connected) {
      const badge = card.querySelector('.connected');
      badge.hidden = false;
      badge.textContent = `connected · ${connected.info.database}`;
      card.querySelector('.close-connected').hidden = false;
    }
    const startup = state.startups.get(container.name);
    if (startup) {
      // The picker needs a database list; the log well does not. A Refresh
      // mid-start leaves probe null, and reading it here once took the whole
      // container list down with a TypeError.
      if (container.probe) {
        fillPicker(card, container);
      }
      const start = card.querySelector('.start');
      const well = card.querySelector('.startup-log');
      start.disabled = !startup.failed;
      start.classList.toggle('busy', !startup.failed);
      start.setAttribute('aria-busy', startup.failed ? 'false' : 'true');
      start.title = startup.failed
        ? 'Open the session. Odoo loads its registry once; then commands are cheap.'
        : 'Opening a session — Odoo is loading its registry.';
      well.hidden = false;
      well.replaceChildren(...startup.view.map(logRow));
      // This card was cloned from the template, so the well's own scroll
      // position is gone. Scrolling up to read a traceback must survive a
      // re-render, and re-renders are not user-initiated — every probe that
      // finishes fires two.
      well.addEventListener('scroll', () => {
        startup.pinned = isLogPinned(well);
        startup.scrollTop = well.scrollTop;
      });
      if (startup.failed && startup.error) {
        note.textContent = startup.error;
        note.classList.add('error');
      } else if (!startup.failed) {
        note.classList.remove('error');
        note.textContent = 'Loading Odoo registry…';
      }
    }
    list.append(fragment);
    if (startup) {
      // Only now is the well measurable. Setting scrollTop while the card is
      // still in the detached fragment is a silent no-op, which left every
      // re-render showing the oldest lines instead of the newest.
      restoreStartupScroll(card, startup);
    }
  });
}

function restoreStartupScroll(card, startup) {
  const well = card.querySelector('.startup-log');
  if (!well) {

    return;
  }
  if (startup.pinned) {
    well.scrollTop = well.scrollHeight;

    return;
  }
  well.scrollTop = startup.scrollTop;
}

async function loadContainers() {
  const list = document.querySelector('#containers');
  list.innerHTML = '<li class="empty">Looking for running containers…</li>';
  try {
    const containers = await request(() => api.get('/api/containers'));
    const last = localStorage.getItem('osContainer');
    state.containers = containers
      .map((container) => ({...container, probe: null, probing: true}))
      .sort((a, b) => (a.name === last ? -1 : b.name === last ? 1 : 0));
    renderContainers();
    await Promise.allSettled(state.containers.map((container) => probeContainer(container.name)));
  } catch (error) {
    list.innerHTML = `<li class="card"><p class="probe-note error">${escapeHtml(error.message)}</p></li>`;
  }
}

async function probeContainer(name) {
  const container = state.containers.find((item) => item.name === name);
  if (!container) {

    return;
  }
  container.probing = true;
  renderContainers();
  try {
    container.probe = await request(() => api.post('/api/probe', {container: name}));
  } catch (error) {
    container.probe = {ok: false, supported: false, error: error.message};
  }
  container.probing = false;
  renderContainers();
}

function closeAllPickers() {
  document.querySelectorAll('#containers .picker').forEach((picker) => {
    picker.hidden = true;
  });
}

function fillPicker(card, container) {
  const picker = card.querySelector('.picker');
  const select = picker.querySelector('.databases');
  const manual = picker.querySelector('.database-manual');
  const startup = state.startups.get(container.name);
  picker.hidden = false;
  select.replaceChildren();
  // Optional throughout: a Refresh blanks every probe until the re-probe lands,
  // and a card mid-start is still rendered in that window.
  const databases = container.probe?.databases || [];
  const preferred = startup?.database
    || container.probe?.db_name
    || localStorage.getItem('osDatabase')
    || databases[0];
  if (databases.length) {
    databases.forEach((database) => {
      select.add(new Option(database, database, false, database === preferred));
    });
    select.hidden = false;
    manual.hidden = true;
  } else {
    select.hidden = true;
    manual.hidden = false;
    manual.value = preferred || '';
  }
}

function openPicker(name) {
  const container = state.containers.find((item) => item.name === name);
  const card = document.querySelector(`.card[data-container="${CSS.escape(name)}"]`);
  if (!container?.probe || !card) {

    return;
  }
  closeAllPickers();
  fillPicker(card, container);
  const manual = card.querySelector('.database-manual');
  if (!manual.hidden) {
    manual.focus();
  }
}

// Opaque and per-attempt: the id the POST will return is unknowable until it
// returns, so the client names its own request and the daemon echoes it back.
function newClientToken() {
  if (window.crypto?.randomUUID) {

    return window.crypto.randomUUID();
  }

  return `pt-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function stopStartup(name) {
  const startup = state.startups.get(name);
  if (startup?.drain) {
    cancelAnimationFrame(startup.drain);
  }
  if (startup?.socket) {
    startup.socket.close();
  }
  state.startups.delete(name);
}

function mergeLogLines(fetched, live) {
  if (!live.length) {

    return fetched.slice();
  }
  if (!fetched.length) {

    return live.slice();
  }
  let overlap = 0;
  const max = Math.min(fetched.length, live.length);
  for (let k = max; k > 0; k -= 1) {
    if (fetched.slice(-k).every((line, i) => line === live[i])) {
      overlap = k;
      break;
    }
  }

  return fetched.concat(live.slice(overlap));
}

function enqueueStartupLine(name, line) {
  const startup = state.startups.get(name);
  if (!startup || startup.failed) {

    return;
  }
  startup.pending.push(line);
  if (!startup.drain) {
    startup.drain = requestAnimationFrame(() => drainStartupLog(name));
  }
}

function drainStartupLog(name) {
  const startup = state.startups.get(name);
  if (!startup) {

    return;
  }
  startup.drain = 0;
  const burst = startup.pending.length;
  const take = Math.max(1, Math.min(8, Math.ceil(burst / 60)));
  for (let i = 0; i < take; i += 1) {
    const line = startup.pending.shift();
    if (line === undefined) {

      break;
    }
    startup.view.push(line);
    appendStartupLine(name, line);
  }
  if (startup.pending.length) {
    startup.drain = requestAnimationFrame(() => drainStartupLog(name));
  }
}

function appendStartupLine(name, line) {
  const card = document.querySelector(`.card[data-container="${CSS.escape(name)}"]`);
  const well = card?.querySelector('.startup-log');
  if (!well) {

    return;
  }
  well.hidden = false;
  const pinned = isLogPinned(well);
  well.append(logRow(line));
  if (pinned) {
    well.scrollTop = well.scrollHeight;
  }
}

function connectStartupSocket(name, sessionId) {
  const startup = state.startups.get(name);
  if (!startup) {

    return;
  }
  if (startup.socket) {
    startup.socket.close();
  }
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const startupSocket = new WebSocket(`${protocol}//${location.host}/ws/sessions/${sessionId}`);
  startup.socket = startupSocket;
  const live = [];
  startupSocket.addEventListener('message', (event) => {
    const current = state.startups.get(name);
    if (current !== startup) {

      return;
    }
    const message = JSON.parse(event.data);
    if (message.kind !== 'stderr') {

      return;
    }
    live.push(message.line);
    if (startup.hydrated) {
      startup.lines.push(message.line);
      enqueueStartupLine(name, message.line);
    }
  });
  request(() => api.get(`/api/sessions/${sessionId}/logs?tail=2000`))
    .then((tail) => {
      if (state.startups.get(name) !== startup) {

        return;
      }
      startup.lines = mergeLogLines(tail.lines || [], live);
      startup.lines.forEach((line) => enqueueStartupLine(name, line));
      startup.hydrated = true;
    })
    .catch(() => {
      if (state.startups.get(name) !== startup) {

        return;
      }
      startup.lines = live.slice();
      startup.lines.forEach((line) => enqueueStartupLine(name, line));
      startup.hydrated = true;
    });
}

function watchStartupFromEvent(info) {
  const startup = state.startups.get(info.container);
  if (!startup || startup.failed || startup.sessionId) {

    return;
  }
  // Container and database do not identify a session — a container may hold
  // several, and an agent can be opening the same target right now. Matching on
  // the target adopted the agent's stream and left our own session looking
  // reattached. The token is the only thing that says "this one is ours".
  if (!startup.token || startup.token !== info.client_token) {

    return;
  }
  startup.sessionId = info.id;
  connectStartupSocket(info.container, info.id);
}

async function adoptStartingSession(name) {
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    const startup = state.startups.get(name);
    if (!startup || startup.failed || startup.sessionId) {

      return;
    }
    try {
      const sessions = await request(() => api.get('/api/sessions'));
      const match = (sessions || []).find((info) => (
        info.client_token === startup.token
        && info.state === 'starting'
        && !state.sessions.has(info.id)
      ));
      if (match) {
        watchStartupFromEvent(match);

        return;
      }
    } catch (_error) {
      // The registry socket can still attach the stream.
    }
    await new Promise((resolve) => window.setTimeout(resolve, 50));
  }
}

// Only whoever called POST /api/sessions sees the exception. A page that
// adopted the stream from `session_starting` learns the outcome here or not at
// all — the session simply stops being listed.
function failStartupById(sessionId, reason) {
  for (const [name, startup] of state.startups) {
    if (startup.sessionId !== sessionId || startup.failed) {
      continue;
    }
    startup.failed = true;
    startup.error = reason || 'the session failed to start';
    if (startup.socket) {
      startup.socket.close();
      startup.socket = null;
    }
    renderContainers();

    return name;
  }

  return null;
}

function startupOwns(info) {
  const startup = state.startups.get(info.container);

  return Boolean(startup && startup.sessionId === info.id && !startup.failed);
}

async function startSession(name) {
  const container = state.containers.find((item) => item.name === name);
  const card = document.querySelector(`.card[data-container="${CSS.escape(name)}"]`);
  const note = card.querySelector('.probe-note');
  const select = card.querySelector('.databases');
  const manual = card.querySelector('.database-manual');
  const database = select.hidden ? manual.value.trim() : select.value;
  if (!database) {
    note.textContent = 'Enter a database name.';
    note.classList.add('error');

    return;
  }
  const previous = state.startups.get(name);
  if (previous?.socket) {
    previous.socket.close();
  }
  const token = newClientToken();
  state.startups.set(name, {
    database,
    token,
    sessionId: null,
    lines: [],
    view: [],
    pending: [],
    drain: 0,
    socket: null,
    failed: false,
    error: null,
    hydrated: false,
    pinned: true,
    scrollTop: 0,
  });
  renderContainers();
  adoptStartingSession(name);
  try {
    const info = await request(() => api.post('/api/sessions', {
      container: name,
      database,
      odoo_bin: container.probe.odoo_bin,
      client_token: token,
    }));
    const opening = state.startups.get(name);
    const lines = opening
      ? (opening.lines.length ? opening.lines : opening.view)
      : [];
    stopStartup(name);
    if (info.write_key) {
      saveKey(info.id, info.write_key);
      delete info.write_key;
    }
    state.activeSession = info.id;
    localStorage.setItem('osContainer', name);
    localStorage.setItem('osDatabase', database);
    attachSession(info);
    const record = state.sessions.get(info.id);
    if (record && lines.length) {
      record.logLines = record.logLines.length
        ? mergeLogLines(lines, record.logLines)
        : lines;
    }
    state.activeSession = info.id;
    showScreen('sessions');
  } catch (error) {
    const startup = state.startups.get(name);
    if (startup) {
      startup.failed = true;
      startup.error = error.message;
      if (startup.socket) {
        startup.socket.close();
        startup.socket = null;
      }
    }
    renderContainers();
  }
}

function attachSession(info, reattached = false) {
  // The registry broadcast for a session this page opened itself arrives before
  // the POST response does. Attaching twice would leave two sockets on one
  // session, so the second call only refreshes what it knows.
  const existing = state.sessions.get(info.id);
  if (existing) {
    existing.info = {...existing.info, ...info};
    noteTesting(existing, existing.info.activity);
    renderContainers();
    renderSessions();

    return;
  }
  const record = newSessionRecord(info);
  record.reattached = reattached;
  if (!reattached) {
    record.openedAt = Date.now();
  }
  state.sessions.set(info.id, record);
  state.activeSession ||= info.id;
  noteTesting(record, info.activity);
  connectSocket(info.id);
  renderContainers();
  renderSessions();
}

function cellsFromHistory(data, previous) {
  // Ordinal, not uid: a live cell becomes restored-<id> on hydrate, and a
  // watcher refetch must not undo a card the human already opened or shut.
  const folds = new Map();
  for (const cell of previous || []) {
    if (!cell.boundary && cell.ordinal) {
      folds.set(cell.ordinal, cell.collapsed);
    }
  }
  const cells = [];
  const entries = data.entries || [];
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry.kind === 'exec') {
      cells.push({
        uid: `restored-${entry.id}`,
        ordinal: entry.ordinal,
        collapsed: folds.has(entry.ordinal)
          ? folds.get(entry.ordinal)
          : Boolean(entry.actor && entry.actor.kind === 'agent'),
        code: entry.code,
        status: entry.status,
        started: 0,
        elapsed: entry.result?.duration || 0,
        result: entry.result,
        abandoned: Boolean(entry.abandoned),
        actor: entry.actor,
      });
    } else if (
      entry.kind === 'commit'
      || entry.kind === 'rollback'
      || entry.kind === 'owner_changed'
      || entry.kind === 'policy_changed'
    ) {
      cells.push({
        boundary: entry.kind,
        from: entry.from,
        to: entry.to,
        allowCommit: entry.allow_commit,
      });
    }
  }

  return cells;
}

function applyHistory(record, data) {
  const opened = data.session && data.session.opened_at;
  if (opened) {
    const start = Date.parse(opened);
    if (!Number.isNaN(start)) {
      record.openedAt = start;
    }
  }
  record.history = data.history || [];
  record.historyIndex = record.history.length;
  const execs = (data.entries || []).filter((entry) => entry.kind === 'exec');
  record.nextOrdinal = execs.length + 1;
  record.cells = cellsFromHistory(data, record.cells);
  record.hydrated = true;
  record.awaitingInFlight = execs.some((entry) => entry.status === 'running');
  if (Array.isArray(data.logs) && data.logs.length) {
    record.logLines = data.logs.flatMap((row) => {
      const line = row && row.line;

      return typeof line === 'string' && line ? [line] : [];
    });
  }
}

async function loadSessionHistory(id, {withLogs = true} = {}) {
  const record = state.sessions.get(id);
  if (!record) {

    return;
  }
  record.historyTicket = (record.historyTicket || 0) + 1;
  const ticket = record.historyTicket;
  if (record.info.state === 'busy') {
    record.awaitingInFlight = true;
  }
  try {
    const data = await request(
      () => api.get(`/api/sessions/${id}/history${withLogs ? '?logs=true' : ''}`),
    );
    if (record.historyTicket !== ticket || !state.sessions.has(id)) {

      return;
    }
    applyHistory(record, data);
  } catch (_error) {
    // Leave the feed empty; renderFeed shows the journal link until hydrate.
  }
  if (record.historyTicket !== ticket || !state.sessions.has(id)) {

    return;
  }
  if (withLogs && !record.logLines.length) {
    try {
      const tail = await request(() => api.get(`/api/sessions/${id}/logs?tail=2000`));
      if (record.historyTicket !== ticket || !state.sessions.has(id)) {

        return;
      }
      record.logLines = tail.lines || [];
    } catch (_error) {
      // Live WS stderr can still fill the panel after this.
    }
  }
  record.unseenLogs = 0;
  renderSessions();
}

function connectSocket(id) {
  const record = state.sessions.get(id);
  if (!record) {

    return;
  }
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${protocol}//${location.host}/ws/sessions/${id}`);
  record.socket = socket;
  socket.addEventListener('open', () => {
    record.socketOffline = false;
    renderSessions();
  });
  socket.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    const current = state.sessions.get(id);
    if (!current) {

      return;
    }
    if (message.kind === 'state') {
      current.info.state = message.state;
      current.info.activity = message.activity ?? null;
      noteTesting(current, current.info.activity);
      if (message.state === 'dead') {
        maybeLoadDeadCause(id, current);
      }
      if (message.state === 'ready' && current.awaitingInFlight) {
        loadSessionHistory(id);
      } else if (message.state === 'ready' && !keyFor(id)) {
        // Watching someone else's session: pull the command that just finished.
        loadSessionHistory(id, {withLogs: false});
      }
    } else if (message.kind === 'owner') {
      current.info.owner = message.owner;
    } else if (message.kind === 'policy') {
      current.info.allow_commit = message.allow_commit;
    } else if (message.kind === 'stderr') {
      // Odoo emits a few hundred lines just starting up. Append the one line
      // instead of re-rendering tabs, panel and the whole cell feed per line.
      current.logLines.push(message.line);
      appendLogLine(current, message.line);

      return;
    }
    renderSessions();
  });
  socket.addEventListener('close', () => {
    const current = state.sessions.get(id);
    if (current) {
      current.socketOffline = true;
      renderSessions();
    }
  });
}

function renderSessions() {
  document.querySelector('#session-count').textContent = state.sessions.size;
  const tabs = document.querySelector('#session-tabs');
  const panels = document.querySelector('#session-panels');
  const list = document.createElement('div');
  list.className = 'session-tab-list';

  state.sessions.forEach((record, id) => {
    const tab = document.createElement('button');
    tab.className = `session-tab mono${id === state.activeSession ? ' active' : ''}`;
    tab.title = 'Show this session.';
    tab.append(`${record.info.container} / ${record.info.database}`);
    if (sessionIsTesting(record)) {
      const lamp = document.createElement('span');
      lamp.className = 'tab-lamp';
      lamp.title = 'A test is running.';
      tab.append(lamp);
    }
    if (record.closing) {
      // Closing waits for the bootstrap to read the frame, which it only does
      // between commands — say so instead of leaving a tab that ignores clicks.
      const pending = document.createElement('span');
      pending.className = 'tab-closing';
      pending.textContent = record.closingForce ? 'killing…' : 'closing…';
      tab.append(pending);
    } else {
      const close = document.createElement('span');
      close.className = 'tab-close';
      close.textContent = '×';
      close.title = 'Close session (⌥-click to force kill)';
      close.addEventListener('click', (event) => {
        event.stopPropagation();
        closeSession(id, event.altKey);
      });
      tab.append(close);
    }
    tab.addEventListener('click', () => {
      state.activeSession = id;
      renderSessions();
    });
    list.append(tab);
  });
  tabs.replaceChildren(list);
  // An empty tab strip is still a 32px bar with its own background and
  // bottom rule — with no tabs in it, that reads as a stray line hanging
  // under the toolbar rather than as a tab strip. Hide it instead.
  tabs.hidden = state.sessions.size === 0;

  const id = state.activeSession;
  const record = id ? state.sessions.get(id) : null;
  if (!record) {
    panels.replaceChildren();
    if (state.sessions.size === 0) {
      // Same slot and treatment as the "Containers" / "Journals" heading on
      // the other two screens, not a message stranded in the middle of an
      // empty pane.
      const heading = document.createElement('h1');
      heading.textContent = 'Sessions';
      const empty = document.createElement('p');
      empty.textContent = 'No sessions yet — open one from Connect.';
      panels.append(heading, empty);
    }

    return;
  }
  if (!record.panel) {
    const fragment = document.querySelector('#session-panel').content.cloneNode(true);
    record.panel = fragment.querySelector('.session');
    bindSessionPanel(record.panel, id, record);
    createEditor(record.panel, id, record);
  } else {
    bindSessionPanel(record.panel, id, record);
  }
  // Re-inserting the panel blurs CodeMirror. Only move it when the active
  // session actually changed (or the panel is new).
  if (record.panel.parentNode !== panels) {
    panels.replaceChildren(record.panel);
    record.editor?.refresh();
  }
}

function bindSessionPanel(panel, id, record) {
  panel.dataset.session = id;
  panel.querySelector('.target').textContent = `${record.info.container} / ${record.info.database}`;
  panel.querySelector('.odoo').textContent = `Odoo ${record.info.odoo || 'loading'}`;
  const sessionId = panel.querySelector('.session-id');
  sessionId.setAttribute('aria-label', `Copy session id ${id}`);
  if (sessionId.dataset.flash !== 'copied') {
    sessionId.textContent = `session ${id}`;
  }
  paintSessionAge(record);
  const testing = sessionIsTesting(record);
  const badge = panel.querySelector('.state');
  const label = testing ? 'testing' : record.info.state;
  badge.textContent = record.socketOffline ? `${label} · socket offline` : label;
  const badgeClasses = ['state', 'badge', testing ? 'testing' : record.info.state];
  if (record.socketOffline) {
    badgeClasses.push('offline');
  }
  badge.className = badgeClasses.join(' ');
  panel.querySelector('.socket-offline').hidden = !record.socketOffline;
  maybeLoadDeadCause(id, record);
  renderDeadCause(panel, record);
  // Keys stay in the grid. Disabled is the refusal, not a missing button —
  // hiding one makes the whole row jump.
  // Ownership is decided by whether this browser holds the write key, not by
  // which buttons are on screen.
  const owner = record.info.owner || {kind: 'human', label: 'browser'};
  const owned = Boolean(keyFor(id));
  const ownerBadge = panel.querySelector('.owner-badge');
  ownerBadge.hidden = owner.kind === 'human' && owned;
  ownerBadge.textContent = owned ? `${owner.kind} · ${owner.label}` : `watching · ${owner.label}`;
  ownerBadge.className = `owner-badge badge ${owner.kind}`;
  panel.classList.toggle('observer', !owned);
  const grant = panel.querySelector('.grant-commit');
  const access = panel.querySelector('.grant-access');
  const interrupt = panel.querySelector('.interrupt');
  grant.disabled = owner.kind !== 'agent';
  grant.setAttribute('aria-pressed', record.info.allow_commit ? 'true' : 'false');
  access.setAttribute('aria-pressed', owner.kind === 'agent' ? 'true' : 'false');
  access.title = owner.kind === 'agent'
    ? 'Grant access — Take ownership back from the agent.'
    : 'Grant access — Hand this session to an agent. You keep watching; you stop typing.';
  interrupt.disabled = record.info.state !== 'busy';

  const accepting = record.info.state === 'ready' && owned;
  const rollback = panel.querySelector('.rollback');
  const commit = panel.querySelector('.commit');
  rollback.disabled = !accepting;
  commit.disabled = !accepting;
  panel.querySelector('.close').disabled = !!record.closing;
  panel.querySelector('.kill').disabled = record.closing && record.closingForce;
  const neu = panel.querySelector('.new');
  neu.disabled = !!record.duplicating;
  neu.classList.toggle('busy', !!record.duplicating);
  neu.setAttribute('aria-busy', record.duplicating ? 'true' : 'false');
  neu.title = record.duplicating
    ? 'Opening a session — Odoo is loading its registry.'
    : 'New — Open another session on the same container and database. Stay on this tab.';
  panel.querySelector('.editor-pane').classList.toggle('locked', !accepting);
  panel.classList.toggle('editor-tall', record.editorTall);
  const height = panel.querySelector('.editor-height');
  height.textContent = record.editorTall ? 'shorter' : 'taller';
  height.title = record.editorTall
    ? 'Return the editor to normal height.'
    : 'Double the editor height.';
  if (!panel.dataset.bound) {
    panel.dataset.bound = 'true';
    sessionId.addEventListener('click', () => {
      copyText(id);
      sessionId.dataset.flash = 'copied';
      sessionId.textContent = 'copied';
      window.setTimeout(() => {
        if (sessionId.dataset.flash !== 'copied') {

          return;
        }
        sessionId.dataset.flash = '';
        sessionId.textContent = `session ${id}`;
      }, 900);
    });
    panel.querySelector('.interrupt').addEventListener('click', () => interruptSession(id));
    panel.querySelector('.rollback').addEventListener('click', () => transaction(id, 'rollback'));
    panel.querySelector('.commit').addEventListener('click', () => transaction(id, 'commit'));
    panel.querySelector('.close').addEventListener('click', () => closeSession(id, false));
    panel.querySelector('.kill').addEventListener('click', () => closeSession(id, true));
    panel.querySelector('.new').addEventListener('click', () => duplicateSession(id));
    panel.querySelector('.grant-access').addEventListener('click', () => {
      const current = record.info.owner || {kind: 'human'};
      if (keyFor(id) && current.kind === 'human') {
        handOver(id);
      } else {
        takeBack(id);
      }
    });
    panel.querySelector('.grant-commit').addEventListener('click', () => {
      grantCommit(id, !Boolean(record.info.allow_commit));
    });
    panel.querySelector('.editor-height').addEventListener('click', () => {
      record.editorTall = !record.editorTall;
      panel.classList.toggle('editor-tall', record.editorTall);
      panel.querySelector('.editor-height').textContent =
        record.editorTall ? 'shorter' : 'taller';
      panel.querySelector('.editor-height').title = record.editorTall
        ? 'Return the editor to normal height.'
        : 'Double the editor height.';
      record.editor?.refresh();
    });
    panel.querySelector('.feed-fold').addEventListener('click', () => {
      const collapse = execCells(record).some((cell) => !cell.collapsed);
      execCells(record).forEach((cell) => {
        cell.collapsed = collapse;
      });
      renderFeed(panel.querySelector('.feed'), id, record);
      record.editor?.refresh();
    });
    bindLogs(panel, record);
  }
  renderLogs(panel, record);
  renderFeed(panel.querySelector('.feed'), id, record);
}

function createEditor(panel, id, record) {
  const oldValue = record.editor ? record.editor.getValue() : '';
  const editor = CodeMirror(panel.querySelector('.editor'), {
    value: oldValue,
    mode: 'python',
    theme: 'pytunnel',
    lineNumbers: true,
    indentUnit: 4,
    tabSize: 4,
    lineWrapping: true,
    extraKeys: {
      'Cmd-Enter': () => runCommand(id),
      'Ctrl-Enter': () => runCommand(id),
      Up: (instance) => historyMove(record, instance, -1),
      Down: (instance) => historyMove(record, instance, 1),
    },
  });
  record.editor = editor;
}

function historyMove(record, editor, direction) {
  const cursor = editor.getCursor();
  const atStart = direction < 0 && cursor.line === 0 && cursor.ch === 0;
  const lastLine = editor.lineCount() - 1;
  const atEnd = direction > 0 && cursor.line === lastLine &&
    cursor.ch === editor.getLine(lastLine).length;
  if ((!atStart && direction < 0) || (!atEnd && direction > 0) || !record.history.length) {
    editor.execCommand(direction < 0 ? 'goLineUp' : 'goLineDown');

    return;
  }
  record.historyIndex = Math.max(0, Math.min(
    record.history.length,
    record.historyIndex + direction,
  ));
  editor.setValue(record.history[record.historyIndex] || '');
  editor.setCursor(direction < 0 ? {line: 0, ch: 0} : {
    line: editor.lineCount() - 1,
    ch: editor.getLine(editor.lineCount() - 1).length,
  });
}

async function runCommand(id, explicitCode) {
  const record = state.sessions.get(id);
  const code = explicitCode === undefined ? record?.editor?.getValue() : explicitCode;
  if (!record || !code?.trim()) {

    return;
  }
  if (record.info.state === 'busy' || record.info.state === 'starting') {

    return;
  }
  const cell = {
    uid: `cell-${(state.cellCounter += 1)}`,
    ordinal: record.nextOrdinal,
    collapsed: false,
    code,
    status: 'running',
    started: performance.now(),
    elapsed: 0,
    result: null,
    actor: record.info.owner,
  };
  record.nextOrdinal += 1;
  record.cells.unshift(cell);
  record.history.push(code);
  record.historyIndex = record.history.length;
  // Only the elapsed counter changes while a command runs. Re-rendering the
  // whole panel four times a second would detach the editor and drop the
  // caret and any text selection.
  const timer = setInterval(() => {
    cell.elapsed = (performance.now() - cell.started) / 1000;
    updateCellDuration(id, cell);
  }, 250);
  record.info.state = 'busy';
  renderSessions();
  try {
    cell.result = await request(
      () => api.post(`/api/sessions/${id}/exec`, {code}, authHeaders(id)),
    );
    cell.status = cell.result.error ? 'error' : 'done';
    record.info.pending_commands = (record.info.pending_commands || 0) + 1;
    if (explicitCode === undefined) {
      record.editor?.setValue('');
    }
  } catch (error) {
    cell.status = 'error';
    if (error.status === 409) {
      cell.result = {error: {type: 'SessionBusy', message: 'session busy', traceback: ''}};
    } else if (error.status === 410) {
      record.info.state = 'dead';
      maybeLoadDeadCause(id, record);
      cell.result = {error: {type: 'SessionDead', message: error.message, traceback: ''}};
    } else if (error.status === 504) {
      cell.result = {
        error: {
          type: 'TimeoutError',
          message: 'Command exceeded its ceiling and was interrupted.',
          traceback: '',
        },
      };
    } else {
      cell.result = {error: {type: 'RequestError', message: error.message, traceback: ''}};
    }
  } finally {
    clearInterval(timer);
    cell.elapsed = (performance.now() - cell.started) / 1000;
    // Ask the daemon rather than guessing: after a timeout the command is
    // still running in the container and the session stays busy.
    await refreshSessionInfo(id);
    renderSessions();
    record.editor?.focus();
  }
}

async function refreshSessionInfo(id) {
  const record = state.sessions.get(id);
  if (!record) {

    return;
  }
  try {
    record.info = {...record.info, ...(await api.get(`/api/sessions/${id}`))};
  } catch (error) {
    if (error.status === 404) {
      record.info.state = 'closed';
    }
  }
}

function updateCellDuration(id, cell) {
  const record = state.sessions.get(id);
  const element = record?.panel?.querySelector(`[data-cell="${cell.uid}"] .cell-duration`);
  if (element) {
    element.textContent = `${cell.elapsed.toFixed(2)}s`;
  }
}

function execCells(record) {
  return record.cells.filter((cell) => !cell.boundary);
}

// A handover is not a transaction boundary, and saying so out loud beats
// printing "Transaction owner_changed".
function markerText(cell) {
  if (cell.boundary === 'owner_changed') {
    const was = cell.from || {};
    const now = cell.to || {};

    return `Handed over — ${was.kind || '?'} (${was.label || '?'})`
      + ` → ${now.kind || '?'} (${now.label || '?'})`;
  }
  if (cell.boundary === 'policy_changed') {

    return cell.allowCommit ? 'Commit granted' : 'Commit revoked';
  }

  return `Transaction ${cell.boundary}`;
}

function renderFeed(feed, id, record) {
  const cards = feed.querySelector('.feed-cards');
  const head = feed.querySelector('.feed-head');
  const fold = feed.querySelector('.feed-fold');
  const execCount = execCells(record).length;
  const allFolded = execCount > 0 && execCells(record).every((cell) => cell.collapsed);
  head.hidden = !execCount;
  fold.classList.toggle('collapse', !allFolded);
  fold.classList.toggle('expand', allFolded);
  fold.title = allFolded ? 'Expand all' : 'Collapse all';
  fold.setAttribute('aria-label', allFolded ? 'Expand all cells' : 'Collapse all cells');
  cards.replaceChildren();
  if (sessionIsTesting(record) && execCount === 0) {
    cards.innerHTML =
      '<p class="empty">Tests are running — open Logs to watch them live.</p>';

    return;
  }
  if (record.reattached && !record.hydrated && !record.cells.length) {
    const note = document.createElement('p');
    note.className = 'history-note';
    note.innerHTML =
      `Live session reattached — <a href="/api/journals/${id}?fmt=markdown" target="_blank">open journal for history</a>`;
    cards.append(note);

    return;
  }
  if (!record.cells.length) {
    cards.innerHTML = '<p class="empty">No commands yet — press ⌘+Enter to run</p>';

    return;
  }
  record.cells.forEach((cell) => {
    if (cell.boundary) {
      const marker = document.createElement('div');
      marker.className = `boundary ${cell.boundary}`;
      marker.textContent = markerText(cell);
      cards.append(marker);

      return;
    }
    const element = document.createElement('article');
    element.className = cell.collapsed ? 'cell folded' : 'cell';
    element.dataset.cell = cell.uid;
    const duration = cell.result?.duration ?? cell.elapsed;
    const ordinal = cell.ordinal ? `#${cell.ordinal}` : '';
    const late = cell.abandoned
      ? '<p class="late-note">late result — the command had exceeded its ceiling</p>'
      : '';
    const body = cell.collapsed ? '' : `
      <pre class="cell-code">${escapeHtml(cell.code)}</pre>
      ${late}${resultHtml(cell.result, id)}`;
    element.innerHTML = `
      <header class="cell-head">
        <button class="cell-fold fold-mark" title="${cell.collapsed ? 'Expand' : 'Collapse'}" aria-label="${cell.collapsed ? 'Expand' : 'Collapse'}"></button>
        <span class="cell-ordinal">${ordinal}</span>
        ${cell.actor ? `<span class="cell-actor">${escapeHtml(cell.actor.label)}</span>` : ''}
        <span class="cell-duration">${Number(duration || 0).toFixed(2)}s</span>
        <span class="sep" aria-hidden="true">/</span>
        <span class="cell-status ${cell.status}">${cell.status === 'running' ? '● running' : cell.status}</span>
        <span class="cell-actions">
          <button class="copy-code" title="Copy this command’s code.">copy code</button>
          <button class="copy-output" title="Copy stdout, the result, and any traceback.">copy output</button>
          <button class="rerun" title="Run this code again as a new command.">re-run</button>
        </span>
      </header>
      ${body}
    `;
    element.querySelector('.cell-fold').classList.add(cell.collapsed ? 'expand' : 'collapse');
    // The dot stays the affordance, but the header around it is the target — a
    // 11px circle is a small thing to hit. It bubbles here, so one listener does
    // both; copy and re-run sit in .cell-actions and must not fold the card.
    element.querySelector('.cell-head').addEventListener('click', (event) => {
      if (event.target.closest('.cell-actions')) {

        return;
      }
      cell.collapsed = !cell.collapsed;
      renderFeed(feed, id, record);
    });
    element.querySelector('.copy-code').addEventListener('click', () => copyText(cell.code));
    element.querySelector('.copy-output').addEventListener('click', () => {
      copyText([cell.result?.stdout, cell.result?.result, cell.result?.error?.traceback]
        .filter(Boolean).join('\n'));
    });
    element.querySelector('.rerun').addEventListener('click', () => runCommand(id, cell.code));
    cards.append(element);
  });
}

function resultHtml(result, id) {
  if (!result) {

    return '';
  }
  let html = '';
  if (result.stdout) {
    html += `<pre class="cell-output stdout">${escapeHtml(result.stdout)}</pre>`;
  }
  if (result.result) {
    html += `<pre class="cell-output result">${escapeHtml(result.result)}</pre>`;
  }
  if (result.error) {
    const error = typeof result.error === 'string' ? {message: result.error} : result.error;
    html += `
      <section class="cell-output error-block">
        <div class="error-type">${escapeHtml(error.type || 'Error')}: ${escapeHtml(error.message || '')}</div>
        ${error.traceback ? `<pre class="traceback">${escapeHtml(error.traceback)}</pre>` : ''}
      </section>`;
  }
  if (result.stdout_truncated || result.result_truncated) {
    html += `<p class="truncated">truncated — <a href="/api/journals/${id}?fmt=markdown" target="_blank">full text in the journal</a></p>`;
  }

  return html;
}

async function copyText(text) {
  const value = text || '';
  try {
    await navigator.clipboard.writeText(value);
  } catch (error) {
    const field = document.createElement('textarea');
    field.value = value;
    field.setAttribute('readonly', '');
    field.style.cssText = 'position:fixed;left:-9999px';
    document.body.append(field);
    field.select();
    try {
      document.execCommand('copy');
    } catch (fallback) {
      console.warn('clipboard unavailable', error);
    }
    field.remove();
  }
}

async function transaction(id, kind) {
  const record = state.sessions.get(id);
  if (!record) {

    return;
  }
  if (kind === 'commit' && !confirm(`Commit changes to ${record.info.database}?`)) {

    return;
  }
  try {
    const result = await request(
      () => api.post(`/api/sessions/${id}/${kind}`, undefined, authHeaders(id)),
    );
    if (result.error) {
      throw new Error(result.error.message || result.error);
    }
    record.info.pending_commands = 0;
    record.cells.unshift({boundary: kind});
    renderSessions();
  } catch (error) {
    alert(`${kind} failed: ${error.message}`);
  }
}

async function interruptSession(id) {
  try {
    await request(
      () => api.post(`/api/sessions/${id}/interrupt`, undefined, authHeaders(id, {admin: true})),
    );
  } catch (error) {
    alert(`Interrupt failed: ${error.message}`);
  }
}

// Only asked for when the daemon actually refuses: acting on your own session
// never needs it.
async function withAdminRetry(operation) {
  try {

    return await request(operation);
  } catch (error) {
    if (error.status !== 403 || !(await ensureAdminKey())) {
      throw error;
    }
    try {

      return await request(operation);
    } catch (retried) {
      if (retried.status === 403) {
        // The stored key is wrong or from an older daemon. Keeping it would
        // make every future attempt fail the same way, with no way back.
        localStorage.removeItem('osAdminKey');
      }
      throw retried;
    }
  }
}

async function ensureAdminKey() {
  if (adminKey()) {

    return true;
  }
  const entered = prompt(
    'Admin key — needed to act on a session you do not own.\n\n'
    + 'The daemon printed it at startup, and keeps it here:\n'
    + '  ~/.odoo-sheller/admin.key\n\n'
    + 'Read it with:  cat ~/.odoo-sheller/admin.key\n'
    + 'No endpoint serves it, so it has to be pasted once.',
  );
  if (!entered) {

    return false;
  }
  localStorage.setItem('osAdminKey', entered.trim());

  return true;
}

async function handOver(id) {
  const record = state.sessions.get(id);
  if (!record) {

    return;
  }
  const pending = record.info.pending_commands || 0;
  const warning = pending
    ? `\n\n${pending} command(s) are uncommitted. They stay in the session and `
      + 'become part of what the agent could commit. Roll back first if that is not '
      + 'what you want.'
    : '';
  const label = prompt(`Hand this session to which agent?${warning}`, 'claude');
  if (!label) {

    return;
  }
  try {
    const result = await withAdminRetry(() => api.post(
      `/api/sessions/${id}/owner`,
      {owner: {kind: 'agent', label}},
      authHeaders(id, {admin: true}),
    ));
    // The key can no longer type, but it still closes: giving work away is not
    // giving up the ability to stop it.
    rememberCloseKey(id, keyFor(id));
    forgetKey(id);
    record.info.owner = result.owner;
    record.info.allow_commit = result.allow_commit;
    renderSessions();
    const payload = JSON.stringify({session_id: id, write_key: result.write_key});
    const accepted = window.prompt(
      'Give this to the agent — shown once, never again.\nOK copies session_id and write_key as JSON.',
      payload,
    );
    if (accepted !== null) {
      await copyText(payload);
    }
  } catch (error) {
    alert(`Hand over failed: ${error.message}`);
  }
}

async function takeBack(id) {
  const record = state.sessions.get(id);
  if (!record) {

    return;
  }
  try {
    const result = await withAdminRetry(() => api.post(
      `/api/sessions/${id}/owner`,
      {owner: {kind: 'human', label: 'browser'}},
      {...authHeaders(id, {admin: true}), 'X-OS-Session-Key': closeKeyFor(id)},
    ));
    saveKey(id, result.write_key);
    forgetCloseKey(id);
    record.info.owner = result.owner;
    record.info.allow_commit = result.allow_commit;
    renderSessions();
  } catch (error) {
    alert(`Take back failed: ${error.message}`);
  }
}

async function grantCommit(id, allowed) {
  const record = state.sessions.get(id);
  if (!record) {

    return;
  }
  if (allowed && !confirm(
    `Let this agent write to ${record.info.database}?\n\n`
    + 'Everything uncommitted in the session, including anything you did before '
    + 'handing it over, would be written.',
  )) {
    renderSessions();

    return;
  }
  try {
    const result = await withAdminRetry(() => api.post(
      `/api/sessions/${id}/policy`,
      {allow_commit: allowed},
      {...authHeaders(id, {admin: true}), 'X-OS-Session-Key': closeKeyFor(id)},
    ));
    record.info.allow_commit = result.allow_commit;
    renderSessions();
  } catch (error) {
    alert(`Policy change failed: ${error.message}`);
    renderSessions();
  }
}

async function closeSession(id, force) {
  const record = state.sessions.get(id);
  if (!record) {

    return;
  }
  // A close already under way may still be escalated to a kill — that is the
  // way out when the bootstrap is stuck in a long command and never reads the
  // close frame. Anything else is a repeat click.
  const escalating = record.closing && force && !record.closingForce;
  if (record.closing && !escalating) {

    return;
  }
  if (!escalating && (record.info.pending_commands || 0) > 0 &&
      !confirm('Uncommitted work will be discarded. Continue?')) {

    return;
  }
  record.closing = true;
  record.closingForce = force;
  renderSessions();
  const send = () => api.del(
    `/api/sessions/${id}${force ? '?force=true' : ''}`,
    {
      ...authHeaders(id, {admin: true}),
      'X-OS-Session-Key': closeKeyFor(id),
    },
  );
  try {
    try {
      await request(send);
    } catch (error) {
      // Closing someone else's live session is an admin act; ask for the key
      // once and try again rather than leaving a tab that cannot be dismissed.
      if (error.status !== 403 || !(await ensureAdminKey())) {
        throw error;
      }
      await request(send);
    }
    record.socket?.close();
    dropTestingTimer(record);
    state.sessions.delete(id);
    forgetKey(id);
    forgetCloseKey(id);
    if (state.activeSession === id) {
      state.activeSession = state.sessions.keys().next().value || null;
    }
    renderContainers();
    renderSessions();
    if (!state.sessions.size) {
      showScreen('connect');
    }
  } catch (error) {
    record.closing = false;
    record.closingForce = false;
    renderSessions();
    alert(`${force ? 'Force kill' : 'Close'} failed: ${error.message}`);
  }
}

async function duplicateSession(id) {
  const record = state.sessions.get(id);
  if (!record || record.duplicating) {

    return;
  }
  record.duplicating = true;
  renderSessions();
  try {
    const container = state.containers.find((item) => item.name === record.info.container);
    const probe = container?.probe?.odoo_bin
      ? container.probe
      : await request(() => api.post('/api/probe', {container: record.info.container}));
    const info = await request(() => api.post('/api/sessions', {
      container: record.info.container,
      database: record.info.database,
      odoo_bin: probe.odoo_bin,
    }));
    if (info.write_key) {
      saveKey(info.id, info.write_key);
      delete info.write_key;
    }
    attachSession(info);
    if (state.activeSession === info.id && state.sessions.has(id)) {
      state.activeSession = id;
    }
  } catch (error) {
    alert(`New session failed: ${error.message}`);
  } finally {
    record.duplicating = false;
    renderSessions();
  }
}

function bindLogs(panel, record) {
  const head = panel.querySelector('.logs-head');
  const lines = panel.querySelector('.log-lines');
  const filter = panel.querySelector('.log-filter');
  const count = panel.querySelector('.log-count');
  const focus = panel.querySelector('.logs-focus');
  head.addEventListener('click', (event) => {
    if (event.target.closest('.log-filter, .logs-focus')) {

      return;
    }
    lines.hidden = !lines.hidden;
    filter.hidden = lines.hidden;
    count.hidden = !lines.hidden;
    if (!lines.hidden) {
      record.unseenLogs = 0;
    } else {
      record.logsFocused = false;
    }
    renderLogs(panel, record);
    record.editor?.refresh();
  });
  focus.addEventListener('click', () => {
    record.logsFocused = !record.logsFocused;
    renderLogs(panel, record);
    record.editor?.refresh();
  });
  filter.addEventListener('change', () => renderLogs(panel, record));
}

async function loadDeadCause(id, record) {
  if (record.deadCauseLoading || record.deadCauseLoaded) {

    return;
  }
  record.deadCauseLoading = true;
  if (!record.logLines.length) {
    try {
      const data = await request(() => api.get(`/api/sessions/${id}/logs`));
      record.logLines.push(...(data.lines || []));
    } catch (_error) {
      // keep whatever stderr we already buffered
    }
  }
  record.deadCauseLoaded = true;
  record.deadCauseLoading = false;
  renderSessions();
}

function maybeLoadDeadCause(id, record) {
  if (record.info.state !== 'dead') {

    return;
  }
  loadDeadCause(id, record);
}

function renderDeadCause(panel, record) {
  const banner = panel.querySelector('.dead-cause');
  const isDead = record.info.state === 'dead';
  banner.hidden = !isDead;
  if (!isDead) {

    return;
  }
  const lines = record.logLines.slice(-DEAD_CAUSE_TAIL);
  panel.querySelector('.dead-cause-lines').textContent =
    lines.length ? lines.join('\n') : 'No stderr captured.';
}

function logRow(line) {
  const row = document.createElement('div');
  row.textContent = line;
  row.className = line.includes('ERROR') ? 'log-error' :
    line.includes('WARNING') ? 'log-warning' : '';

  return row;
}

// Follow the tail only when the reader is already at the bottom. Scrolling up
// to read a traceback must not be undone by the next line — which is what the
// old "freeze scroll" checkbox was for.
function isLogPinned(lines) {

  return lines.scrollHeight - lines.scrollTop - lines.clientHeight < LOG_PIN_SLACK;
}

function updateLogCount(panel, record) {
  const count = panel.querySelector('.log-count');
  // slice(-0) returns the whole array, so an empty tail has to be explicit.
  const unseen = record.unseenLogs ? record.logLines.slice(-record.unseenLogs) : [];
  count.textContent = `${record.unseenLogs} new`;
  count.classList.toggle(
    'alert',
    unseen.some((line) => line.includes('WARNING') || line.includes('ERROR')),
  );
}

function appendLogLine(record, line) {
  const panel = record.panel;
  const lines = panel?.querySelector('.log-lines');
  const open = Boolean(lines) && !lines.hidden;
  if (!open) {
    record.unseenLogs += 1;
  }
  if (!panel) {

    return;
  }
  updateLogCount(panel, record);
  if (!open) {

    return;
  }
  const filter = panel.querySelector('.log-filter');
  if (filter.value && !line.includes(filter.value)) {

    return;
  }
  const pinned = isLogPinned(lines);
  lines.append(logRow(line));
  if (pinned) {
    lines.scrollTop = lines.scrollHeight;
  }
}

function renderLogs(panel, record) {
  const lines = panel.querySelector('.log-lines');
  const filter = panel.querySelector('.log-filter');
  const focus = panel.querySelector('.logs-focus');
  const close = panel.querySelector('.logs-close');
  const open = !lines.hidden;
  panel.classList.toggle('logs-open', open);
  panel.classList.toggle('logs-focused', open && record.logsFocused);
  focus.hidden = !open;
  close.hidden = !open;
  focus.classList.toggle('expand', !record.logsFocused);
  focus.classList.toggle('collapse', record.logsFocused);
  focus.title = record.logsFocused
    ? 'Bring the editor and cell feed back.'
    : 'Hide the editor and cell feed so the log fills the pane.';
  focus.setAttribute(
    'aria-label',
    record.logsFocused ? 'Collapse logs' : 'Expand logs',
  );
  updateLogCount(panel, record);
  const pinned = isLogPinned(lines);
  lines.replaceChildren();
  record.logLines
    .filter((line) => !filter.value || line.includes(filter.value))
    .forEach((line) => lines.append(logRow(line)));
  if (pinned) {
    lines.scrollTop = lines.scrollHeight;
  }
}

async function loadJournals() {
  const groupsElement = document.querySelector('#journal-groups');
  groupsElement.innerHTML = '<p>Loading journals…</p>';
  applyJournalLayout(0);
  try {
    const journals = await request(() => api.get('/api/journals'));
    state.journals = journals;
    const groups = new Map();
    journals.forEach((entry) => {
      const key = `${entry.container}\u0000${entry.database}`;
      if (!groups.has(key)) {
        groups.set(key, []);
      }
      groups.get(key).push(entry);
    });
    groupsElement.replaceChildren();
    if (!groups.size) {
      groupsElement.innerHTML = '<p>No journals yet.</p>';
      applyJournalLayout(0);

      return;
    }
    groupsElement.append(journalColumns());
    groups.forEach((entries) => {
      const group = document.createElement('section');
      group.className = 'journal-group';
      const key = `${entries[0].container}\u0000${entries[0].database}`;
      const live = [...state.sessions.values()].some((record) =>
        record.info.container === entries[0].container &&
        record.info.database === entries[0].database);
      group.innerHTML = `
        <header>
          <h2 class="mono">${escapeHtml(entries[0].container)} / ${escapeHtml(entries[0].database)}</h2>
          <span class="journal-meta">${entries.length} session${entries.length === 1 ? '' : 's'} · last ${escapeHtml(formatStamp(entries[0].opened_at))}</span>
          ${live ? '<span class="journal-live">live</span>' : ''}
        </header>`;
      const header = group.querySelector('header');
      header.title = 'Hide or show journals for this container and database.';
      header.tabIndex = 0;
      header.setAttribute('role', 'button');
      header.setAttribute('aria-expanded', String(state.expandedJournalGroups.has(key)));
      if (!state.expandedJournalGroups.has(key)) {
        group.classList.add('collapsed');
      }
      header.addEventListener('click', () => {
        toggleJournalGroup(group, key);
      });
      header.addEventListener('keydown', (event) => {
        if (event.target.closest('.journal-delete')) {

          return;
        }
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          toggleJournalGroup(group, key);
        }
      });
      const finished = entries.filter((entry) => !state.sessions.has(entry.session_id));
      if (finished.length) {
        header.append(journalTrashButton(
          'journal-delete journal-group-delete',
          'Delete finished journals in this group.',
          () => deleteJournalGroup(
            entries, entries[0].container, entries[0].database,
          ),
        ));
      }
      entries.forEach((entry) => group.append(journalRow(entry)));
      groupsElement.append(group);
    });
    applyJournalLayout(journals.length);
    highlightJournalRow(state.activeJournalId);
  } catch (error) {
    groupsElement.innerHTML = `<p class="probe-note error">${escapeHtml(error.message)}</p>`;
    applyJournalLayout(0);
  }
}

function toggleJournalGroup(group, key) {
  const expanded = state.expandedJournalGroups.has(key);
  if (expanded) {
    state.expandedJournalGroups.delete(key);
  } else {
    state.expandedJournalGroups.add(key);
  }
  group.classList.toggle('collapsed', expanded);
  group.querySelector('header').setAttribute('aria-expanded', String(!expanded));
}

function applyJournalLayout(rowCount) {
  const screen = document.querySelector('#screen-journals');
  const count = screen.querySelector('.journal-index-count');
  const preview = screen.querySelector('#journal-preview');
  const rows = rowCount == null
    ? screen.querySelectorAll('.journal-row').length
    : rowCount;
  const previewOpen = Boolean(state.activeJournalId) && !preview.hidden;
  screen.classList.toggle('preview-open', previewOpen);
  // The heading already says Journals; the badge counts what is under it.
  count.textContent = `/ ${rows} row${rows === 1 ? '' : 's'}`;
  count.hidden = !rows;
}

function highlightJournalRow(id) {
  document.querySelectorAll('.journal-row').forEach((row) => {
    row.classList.toggle('active', Boolean(id) && row.dataset.journal === id);
  });
  if (id) {
    document.querySelector(`.journal-row[data-journal="${CSS.escape(id)}"]`)
      ?.scrollIntoView({block: 'nearest'});
  }
}

function journalOwner(entry) {
  const seen = entry.owners_seen || [];
  const last = entry.owner || seen[seen.length - 1];
  if (!last) {
    // Journals written before sessions had owners.
    return {kind: 'unknown', text: '—'};
  }
  const kinds = [...new Set(seen.map((one) => one.kind))];
  if (kinds.length > 1) {
    // A handover: say so, the transcript is not one actor's work.
    return {kind: 'shared', text: kinds.join('→')};
  }

  return {kind: last.kind, text: last.kind};
}

async function copyJournal(id, button) {
  const label = button.textContent;
  button.disabled = true;
  try {
    const response = await fetch(`/api/journals/${id}?fmt=markdown`);
    if (!response.ok) {
      throw new Error(response.statusText);
    }
    await copyText(await response.text());
    button.textContent = 'copied';
  } catch (error) {
    button.textContent = 'failed';
    console.warn('could not copy journal', error);
  } finally {
    button.disabled = false;
    window.setTimeout(() => {
      button.textContent = label;
    }, 1200);
  }
}

// Six mono columns and a pile of export links say nothing about themselves.
// Sticky, so it is still there once you have scrolled into a long container.
function journalColumns() {
  const header = document.createElement('div');
  header.className = 'journal-columns';
  header.innerHTML = `
    <span>opened</span>
    <span>owner</span>
    <span>session</span>
    <span>duration</span>
    <span>commands</span>
    <span>outcome</span>
    <span class="journal-export-links">export</span>
    <span class="journal-delete-col"></span>`;

  return header;
}

function journalRow(entry) {
  const row = document.createElement('div');
  row.className = 'journal-row';
  row.tabIndex = 0;
  row.dataset.journal = entry.session_id;
  row.title = 'Preview this session’s transcript.';
  const disposition = entry.committed ? 'committed' : 'discarded';
  const commands = `${entry.commands} command${entry.commands === 1 ? '' : 's'}`;
  const owner = journalOwner(entry);
  row.innerHTML = `
    <span class="journal-stamp">${escapeHtml(formatStamp(entry.opened_at))}</span>
    <span class="journal-owner ${owner.kind}">${escapeHtml(owner.text)}</span>
    <span class="journal-id">${escapeHtml(entry.session_id)}</span>
    <span>${escapeHtml(formatDuration(entry.duration))}</span>
    <span>${commands}</span>
    <span class="journal-status ${disposition}">${disposition}</span>
    <span class="journal-export-links"></span>`;
  const exportLinks = row.querySelector('.journal-export-links');
  const jsonlLink = document.createElement('a');
  jsonlLink.href = `/api/journals/${entry.session_id}?fmt=jsonl`;
  jsonlLink.target = '_blank';
  jsonlLink.textContent = '.jsonl';
  jsonlLink.title = 'Export this session as JSONL. Journals are unmasked.';
  bindJournalExportLink(jsonlLink, '.jsonl');
  const mdLink = document.createElement('a');
  mdLink.href = `/api/journals/${entry.session_id}?fmt=markdown`;
  mdLink.target = '_blank';
  mdLink.textContent = '.md';
  mdLink.title = 'Export this session as Markdown. Journals are unmasked.';
  bindJournalExportLink(mdLink, '.md');
  const copyButton = document.createElement('button');
  copyButton.className = 'journal-copy';
  copyButton.textContent = 'copy';
  copyButton.title = `Copy the transcript to the clipboard. ${JOURNAL_UNMASKED_WARNING}`;
  copyButton.addEventListener('click', (event) => {
    event.stopPropagation();  // the row itself opens the preview
    copyJournal(entry.session_id, copyButton);
  });
  exportLinks.append(
    jsonlLink, document.createTextNode(' · '), mdLink, document.createTextNode(' · '), copyButton,
  );
  if (state.sessions.has(entry.session_id)) {
    const slot = document.createElement('span');
    slot.className = 'journal-delete-col';
    row.append(slot);
  } else {
    row.append(journalTrashButton(
      'journal-delete',
      'Delete this journal from disk.',
      () => deleteJournal(entry.session_id),
    ));
  }
  const open = () => previewJournal(entry.session_id);
  row.addEventListener('click', (event) => {
    if (event.target.closest('a, button')) {

      return;
    }
    open();
  });
  row.addEventListener('keydown', (event) => {
    if (event.target.closest('a, button')) {

      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      open();
    }
  });

  return row;
}

function journalTitle(id) {
  const entry = (state.journals || []).find((one) => one.session_id === id);
  if (!entry) {

    return id;
  }

  return `${entry.session_id} · ${entry.container} / ${entry.database}`
    + ` · ${formatStamp(entry.opened_at)}`;
}

function setJournalFocus(focused) {
  state.journalFocused = focused;
  const screen = document.querySelector('#screen-journals');
  screen.classList.toggle('journal-focused', focused);
  const button = screen.querySelector('.journal-focus');
  button.classList.toggle('expand', !focused);
  button.classList.toggle('collapse', focused);
  button.title = focused
    ? 'Bring the journal list back.'
    : 'Hide the list and give the transcript the whole pane.';
  button.setAttribute(
    'aria-label',
    focused ? 'Collapse journal' : 'Expand journal',
  );
}

function journalTrashButton(className, title, onClick) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = className;
  button.title = title;
  button.setAttribute('aria-label', title);
  button.innerHTML = '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M6 1h4l1 2h3v1H2V3h3l1-2zm-2 4h8l-.6 9H4.6L4 5zm2 1v7h1V6H6zm3 0v7h1V6H9z"/></svg>';
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    onClick();
  });

  return button;
}

// Returns null on success, the reason otherwise. The trash button drops the
// promise, so a refusal has to be reported here or it is lost: a live session
// answers 409 and the row simply stayed put.
async function deleteJournal(id, {reload = true, report = true} = {}) {
  try {
    await withAdminRetry(
      () => api.del(`/api/journals/${id}`, authHeaders(null, {admin: true})),
    );
  } catch (error) {
    if (report) {
      alert(`Delete failed: ${error.message}`);
    }

    return error.message;
  }
  if (state.activeJournalId === id) closeJournalPreview();
  if (reload) await loadJournals();

  return null;
}

async function deleteJournalGroup(entries, container, database) {
  const ids = entries
    .filter((entry) => !state.sessions.has(entry.session_id))
    .map((entry) => entry.session_id);
  if (!ids.length) return;
  const n = ids.length;
  const noun = n === 1 ? 'journal' : 'journals';
  if (!confirm(`Delete ${n} ${noun} for ${container} / ${database}? Live sessions are kept.`)) return;
  // Keep going past a refusal — deleting what can be deleted is the point —
  // then report the batch once instead of one dialog per id.
  const failures = [];
  try {
    for (const id of ids) {
      const failed = await deleteJournal(id, {reload: false, report: false});
      if (failed) failures.push(`${id} — ${failed}`);
    }
  } finally {
    await loadJournals();
  }
  if (failures.length) {
    alert(`${failures.length} of ${n} could not be deleted:\n${failures.join('\n')}`);
  }
}

function closeJournalPreview() {
  state.activeJournalId = null;
  setJournalFocus(false);
  document.querySelector('#journal-view').hidden = true;
  const preview = document.querySelector('#journal-preview');
  preview.hidden = true;
  preview.textContent = '';
  document.querySelector('.journal-view-title').textContent = '';
  highlightJournalRow(null);
  applyJournalLayout();
}

async function previewJournal(id) {
  const preview = document.querySelector('#journal-preview');
  document.querySelector('#journal-view').hidden = false;
  document.querySelector('.journal-view-title').textContent = journalTitle(id);
  state.activeJournalId = id;
  preview.hidden = false;
  highlightJournalRow(id);
  applyJournalLayout();
  preview.textContent = 'Loading transcript…';
  try {
    const response = await fetch(`/api/journals/${id}?fmt=markdown`);
    if (!response.ok) {
      const detail = (await response.json().catch(() => ({}))).detail || response.statusText;
      preview.textContent = `Could not load journal: ${detail}`;

      return;
    }
    preview.textContent = await response.text();
  } catch (error) {
    setOffline(true);
    preview.textContent = `Could not load journal: ${error.message}`;
  }
}

function formatDuration(seconds) {
  if (seconds == null) {

    return '—';
  }

  return `${Number(seconds).toFixed(1)}s`;
}

function formatStamp(value) {
  if (!value) {

    return 'unknown';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {

    return 'unknown';
  }
  const pad = (part) => String(part).padStart(2, '0');

  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatClock(date) {
  const pad = (part) => String(part).padStart(2, '0');

  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function paintSessionAge(record) {
  const wrap = record.panel?.querySelector('.session-opened');
  const age = record.panel?.querySelector('.session-age');
  if (!wrap || !age) {

    return;
  }
  if (!record.openedAt) {
    wrap.hidden = true;

    return;
  }
  wrap.hidden = false;
  const date = new Date(record.openedAt);
  const seconds = Math.max(0, Math.floor((Date.now() - record.openedAt) / 1000));
  age.textContent = `${formatClock(date)} (${seconds}s)`;
  age.title = formatStamp(date.toISOString());
}

function tickSessionAges() {
  for (const record of state.sessions.values()) {
    paintSessionAge(record);
  }
}

// The registry socket is how a session opened by someone else — an agent, or
// another tab — shows up here without a reload.
function connectRegistrySocket() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${protocol}//${location.host}/ws/sessions`);
  socket.addEventListener('message', async (event) => {
    const message = JSON.parse(event.data);
    if (message.kind === 'session_starting') {
      watchStartupFromEvent(message.session);

      return;
    }
    if (message.kind === 'session_failed') {
      failStartupById(message.session, message.reason);

      return;
    }
    if (message.kind === 'session_opened') {
      const info = message.session;
      if (startupOwns(info)) {

        return;
      }
      if (!state.sessions.has(info.id)) {
        attachSession(info, true);
        loadSessionHistory(info.id);
      }

      return;
    }
    if (message.kind === 'session_closed' && state.sessions.has(message.session)) {
      const record = state.sessions.get(message.session);
      record.socket?.close();
      dropTestingTimer(record);
      state.sessions.delete(message.session);
      forgetKey(message.session);
      if (state.activeSession === message.session) {
        state.activeSession = state.sessions.keys().next().value || null;
      }
      renderContainers();
      renderSessions();

      return;
    }
    const record = state.sessions.get(message.session);
    if (!record) {

      return;
    }
    if (message.kind === 'owner') {
      record.info.owner = message.owner;
    } else if (message.kind === 'policy') {
      record.info.allow_commit = message.allow_commit;
    } else if (message.kind === 'state') {
      record.info.state = message.state;
      record.info.activity = message.activity ?? null;
      noteTesting(record, record.info.activity);
    }
    renderSessions();
  });
  socket.addEventListener('close', () => {
    // The daemon went away or restarted; try again while the page is open.
    window.setTimeout(connectRegistrySocket, 3000);
  });
}

async function reattachSessions() {
  try {
    const sessions = await request(() => api.get('/api/sessions'));
    sessions.forEach((info) => attachSession(info, true));
    await Promise.all(sessions.map((info) => loadSessionHistory(info.id)));
  } catch (_error) {
    // The standing offline banner is the retry signal.
  }
}

document.querySelectorAll('#top [data-screen]').forEach((button) => {
  button.addEventListener('click', () => showScreen(button.dataset.screen));
});
document.querySelector('#refresh').addEventListener('click', loadContainers);
document.querySelector('.journal-focus').addEventListener('click', () => {
  setJournalFocus(!state.journalFocused);
});
document.querySelector('.journal-close').addEventListener('click', () => {
  closeJournalPreview();
});

const TOP_SCREENS = ['connect', 'sessions', 'journals'];

// ⇧+←/→ cycles Connect / Sessions / Journals, wrapping around. Shift-arrow is
// also how text selection extends in a field, so this only fires outside an
// editable one — typing or selecting in the code editor or a text input keeps
// that native behavior.
document.addEventListener('keydown', (event) => {
  if (!event.shiftKey || event.altKey || event.metaKey || event.ctrlKey) {

    return;
  }
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') {

    return;
  }
  if (event.target?.closest?.('input, textarea, select, [contenteditable], .CodeMirror')) {

    return;
  }
  event.preventDefault();
  const step = event.key === 'ArrowRight' ? 1 : -1;
  const index = TOP_SCREENS.indexOf(state.screen);
  const next = TOP_SCREENS[(index + step + TOP_SCREENS.length) % TOP_SCREENS.length];
  showScreen(next);
});

function restoreScreen() {
  const saved = localStorage.getItem('osScreen');
  if (saved === 'connect' || saved === 'sessions' || saved === 'journals') {
    showScreen(saved);
  }
}

connectRegistrySocket();
restoreScreen();
setInterval(tickSessionAges, 1000);
Promise.allSettled([loadContainers(), reattachSessions()]);
