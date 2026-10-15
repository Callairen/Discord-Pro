// API and WebSocket client layer. Views never call fetch() directly.

export class ApiError extends Error {
  constructor(status, body) {
    const err = (body && (body.error || (body.result && body.result.error))) || {};
    super(err.message || `Request failed (${status})`);
    this.status = status;
    this.code = err.code || "http_" + status;
    this.details = err.details || [];
    this.body = body;
  }
}

async function request(method, path, body) {
  const opts = {
    method,
    credentials: "same-origin",
    headers: { "X-Discord-Pro": "1" },
  };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new ApiError(0, { error: { code: "network_error", message: "Backend unreachable" } });
  }
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) {
    // Presence commands return a result body even on failure.
    if (data && data.result) return data;
    throw new ApiError(res.status, data);
  }
  return data;
}

export const api = {
  session: () => request("GET", "/api/v1/auth/session"),
  login: (access_code) => request("POST", "/api/v1/auth/login", { access_code }),
  logout: () => request("POST", "/api/v1/auth/logout"),
  state: () => request("GET", "/api/v1/state"),
  config: () => request("GET", "/api/v1/config"),
  updateConfig: (patch) => request("PUT", "/api/v1/config", patch),
  enable: () => request("POST", "/api/v1/presence/enable"),
  disable: () => request("POST", "/api/v1/presence/disable"),
  updatePresence: (activity, apply, client_id) =>
    request("PUT", "/api/v1/presence", { activity, apply, ...(client_id !== undefined ? { client_id } : {}) }),
  applyPresence: () => request("POST", "/api/v1/presence/apply"),
  presets: () => request("GET", "/api/v1/presets"),
  createPreset: (p) => request("POST", "/api/v1/presets", p),
  updatePreset: (id, p) => request("PUT", `/api/v1/presets/${encodeURIComponent(id)}`, p),
  deletePreset: (id) => request("DELETE", `/api/v1/presets/${encodeURIComponent(id)}`),
  duplicatePreset: (id) => request("POST", `/api/v1/presets/${encodeURIComponent(id)}/duplicate`),
  activatePreset: (id, enable) => request("POST", `/api/v1/presets/${encodeURIComponent(id)}/activate`, { enable }),
  setupValidate: (client_id, activity) => request("POST", "/api/v1/setup/validate", { client_id, activity }),
  setupTest: (client_id) => request("POST", "/api/v1/setup/test-connection", { client_id }),
  setupComplete: (client_id, activity, test_activation) =>
    request("POST", "/api/v1/setup/complete", { client_id, activity, test_activation }),
  diagnostics: () => request("GET", "/api/v1/diagnostics"),
};

// Real-time channel with bounded reconnect and full resync on every connect.
export class LiveSocket {
  constructor({ onEvent, onStatus, onAuthLost, checkAuth }) {
    this.checkAuth = checkAuth || (async () => true);
    this.onEvent = onEvent;
    this.onStatus = onStatus;
    this.onAuthLost = onAuthLost;
    this.attempt = 0;
    this.maxAttempts = 20;
    this.ws = null;
    this.timer = null;
    this.closedByUser = false;
  }

  connect() {
    clearTimeout(this.timer);
    this.closedByUser = false;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/v1/ws`);
    this.ws = ws;
    this.onStatus("connecting", this.attempt);
    ws.onopen = () => { this.attempt = 0; this.onStatus("open", 0); };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      this.onEvent(msg);
    };
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.closedByUser) return;
      if (ev.code === 4401 || ev.code === 4403) { this.onAuthLost(); return; }
      this.scheduleReconnect();
    };
  }

  async scheduleReconnect() {
    // A closed socket may mean the session ended (e.g. backend restarted).
    if (!(await this.checkAuth())) { this.onAuthLost(); return; }
    if (this.closedByUser) return;
    this.attempt += 1;
    if (this.attempt > this.maxAttempts) { this.onStatus("gave_up", this.attempt); return; }
    const delay = Math.min(30000, 1000 * 2 ** (this.attempt - 1)) * (0.8 + Math.random() * 0.4);
    this.onStatus("waiting", this.attempt, delay);
    this.timer = setTimeout(() => this.connect(), delay);
  }

  retryNow() { this.attempt = 0; this.connect(); }

  close() {
    this.closedByUser = true;
    clearTimeout(this.timer);
    if (this.ws) this.ws.close();
  }
}
