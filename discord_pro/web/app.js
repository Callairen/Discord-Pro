// Discord-Pro dashboard. State comes only from the backend (REST + WebSocket);
// nothing is persisted in the browser.
import { api, ApiError, LiveSocket } from "./api.js";

const store = { state: null, config: null, presets: [], ws: "connecting", authed: false };
const listeners = new Set();
let socket = null;
let currentView = null;

// ------------------------------------------------------------------ helpers
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function toast(message, kind = "info", ms = 4500) {
  const el = h("div", { class: `toast ${kind}`, role: kind === "err" ? "alert" : "status" }, message);
  document.getElementById("toasts").append(el);
  setTimeout(() => el.remove(), ms);
}

function fmtTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleTimeString();
}

function fmtAgo(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}

function emit(kind) { for (const fn of listeners) fn(kind); }

function setState(s, { force = false } = {}) {
  if (!s) return;
  if (!force && store.state && s.revision < store.state.revision &&
      s.application.started_at === store.state.application.started_at) return; // stale response
  store.state = s;
  emit("state");
}

function resultToast(body, okText) {
  const r = body && body.result;
  if (!r) return;
  if (body.state) setState(body.state);
  if (r.success) toast(r.message || okText || "Done", "ok");
  else toast(`${r.error.message}${r.pending ? " — still retrying in the background" : ""}`, r.pending ? "warn" : "err", 7000);
}

function handleError(e) {
  if (e instanceof ApiError && e.status === 401) { showLogin(); return; }
  const extra = e.details && e.details.length ? `: ${e.details.map(d => `${d.field} ${d.message}`).join("; ")}` : "";
  toast(`${e.message}${extra}`, "err", 7000);
}

async function withBusy(button, fn) {
  const prev = button.disabled;
  button.disabled = true;
  try { return await fn(); } catch (e) { handleError(e); } finally { button.disabled = prev; }
}

// --------------------------------------------------------- status helpers
const STATE_TEXT = {
  CONNECTED: "Live on Discord", CONNECTING: "Connecting…", RECONNECTING: "Reconnecting…",
  ERROR: "Error", STOPPING: "Stopping…", DISCONNECTED: "Off",
};
const STATE_ICON = { CONNECTED: "●", CONNECTING: "◐", RECONNECTING: "◐", ERROR: "✕", STOPPING: "◌", DISCONNECTED: "○" };

function describe(s) {
  const p = s.presence, d = s.discord;
  if (p.enabled && p.connection_state === "DISCONNECTED") return "Starting…";
  if (!p.enabled && p.connection_state === "DISCONNECTED") return "Off (disabled)";
  if (p.connection_state === "RECONNECTING")
    return `Reconnecting (attempt ${d.reconnect_attempt}${d.next_retry_in != null ? `, next in ${Math.ceil(d.next_retry_in)}s` : ""})`;
  if (p.connection_state === "ERROR") return "Error — waiting for you";
  return STATE_TEXT[p.connection_state] || p.connection_state;
}

function renderPill() {
  const pill = document.getElementById("conn");
  const text = pill.querySelector(".pill-text"), icon = pill.querySelector(".pill-icon");
  if (store.ws !== "open" || !store.state) {
    pill.dataset.state = store.ws === "open" ? "DISCONNECTED" : "OFFLINE";
    icon.textContent = "✕";
    text.textContent = store.ws === "gave_up" ? "Backend offline" : "Connecting to backend…";
    return;
  }
  const cs = store.state.presence.connection_state;
  pill.dataset.state = cs;
  icon.textContent = STATE_ICON[cs] || "○";
  text.textContent = describe(store.state);
}

function renderBanner() {
  const b = document.getElementById("banner");
  b.replaceChildren();
  b.className = "banner";
  if (store.ws === "waiting" || store.ws === "connecting") {
    if (!store.state) { b.hidden = true; return; }
    b.append("Live updates interrupted — reconnecting…");
    b.hidden = false;
  } else if (store.ws === "gave_up") {
    b.classList.add("err");
    b.append("Cannot reach the backend. ", h("button", { onclick: () => socket.retryNow() }, "Retry now"));
    b.hidden = false;
  } else if (store.state && store.state.server.restart_required) {
    b.append("Network settings changed — restart the backend to apply them.");
    b.hidden = false;
  } else {
    b.hidden = true;
  }
}

// -------------------------------------------------------- activity form
const TYPES = [[0, "Playing"], [2, "Listening to"], [3, "Watching"], [5, "Competing in"]];
const FIELD_DEFS = [
  ["details", "Details (line 1)", "e.g. Coding Session — 2–128 characters"],
  ["state", "State (line 2)", "e.g. Visual Studio Code — 2–128 characters"],
  ["large_image", "Large image", "Art Asset key from the Developer Portal, or an https:// image URL"],
  ["large_text", "Large image tooltip", ""],
  ["small_image", "Small image", "Asset key or https:// URL"],
  ["small_text", "Small image tooltip", ""],
];

function activityForm(activity, prefix = "act") {
  const inputs = {};
  const errors = {};
  const rows = [];
  const typeSel = h("select", { id: `${prefix}-activity_type` },
    TYPES.map(([v, l]) => h("option", { value: v }, l)));
  inputs.activity_type = typeSel;
  rows.push(row(`${prefix}-activity_type`, "Activity type", typeSel, "activity_type"));
  for (const [key, label, hint] of FIELD_DEFS) {
    const input = h("input", { type: "text", id: `${prefix}-${key}`, maxlength: key.endsWith("image") ? 256 : 128, autocomplete: "off" });
    inputs[key] = input;
    rows.push(row(`${prefix}-${key}`, label, input, key, hint));
  }
  for (const i of [1, 2]) {
    const l = h("input", { type: "text", id: `${prefix}-b${i}l`, maxlength: 32, placeholder: "Label (max 32)" });
    const u = h("input", { type: "text", id: `${prefix}-b${i}u`, maxlength: 512, placeholder: "https://…" });
    inputs[`button${i}_label`] = l; inputs[`button${i}_url`] = u;
    rows.push(row(`${prefix}-b${i}l`, `Button ${i}`, h("div", { class: "stack" }, l, u), `button${i}`,
      i === 1 ? "Buttons are visible to other people, not on your own profile." : ""));
  }
  const elapsed = h("input", { type: "checkbox", id: `${prefix}-show_elapsed` });
  inputs.show_elapsed = elapsed;
  rows.push(row(`${prefix}-show_elapsed`, "Elapsed timer", h("label", { class: "check" }, elapsed, "Show time since activation"), "show_elapsed"));

  function row(id, label, control, key, hint) {
    const err = h("div", { class: "field-error", hidden: true });
    errors[key] = err;
    return h("div", { class: "row" }, h("label", { for: id }, label),
      h("div", {}, control, hint ? h("div", { class: "hint" }, hint) : null, err));
  }

  function set(a) {
    a = a || {};
    typeSel.value = String(a.activity_type ?? 0);
    for (const [key] of FIELD_DEFS) inputs[key].value = a[key] || "";
    const b = a.buttons || [];
    for (const i of [1, 2]) {
      inputs[`button${i}_label`].value = (b[i - 1] && b[i - 1].label) || "";
      inputs[`button${i}_url`].value = (b[i - 1] && b[i - 1].url) || "";
    }
    elapsed.checked = a.show_elapsed !== false;
    initial = JSON.stringify(read());
  }

  function read() {
    const out = { activity_type: Number(typeSel.value) };
    for (const [key] of FIELD_DEFS) out[key] = inputs[key].value.trim();
    out.buttons = [];
    for (const i of [1, 2]) {
      const label = inputs[`button${i}_label`].value.trim(), url = inputs[`button${i}_url`].value.trim();
      if (label || url) out.buttons.push({ label, url });
    }
    out.show_elapsed = elapsed.checked;
    return out;
  }

  function setErrors(details = []) {
    for (const e of Object.values(errors)) { e.hidden = true; e.textContent = ""; }
    for (const d of details) {
      let f = String(d.field || "").replace(/^(body\.)?(activity|presence|preset\.activity)\./, "");
      const m = f.match(/^buttons\.(\d)/);
      let key = m ? `button${Number(m[1]) + 1}` : f.split(".")[0];
      if (key === "buttons") key = "button1";
      const target = errors[key] || errors.details;
      if (target) { target.hidden = false; target.textContent = d.message.replace(/^Value error, /, ""); }
    }
  }

  let initial = "";
  set(activity);
  const el = h("div", {}, rows);
  return { el, set, read, setErrors, dirty: () => JSON.stringify(read()) !== initial, inputs };
}

function preview(a, title = "Preview") {
  if (!a) return h("div", { class: "card" }, h("div", { class: "label" }, title), h("p", { class: "muted" }, "Nothing published."));
  const img = a.large_image || (a.assets && a.assets.large_image);
  const imgEl = h("div", { class: "img" },
    img && /^https?:\/\//.test(img) ? h("img", { src: img, alt: a.large_text || "large image" }) : (img ? img : "no image"));
  const btns = (a.buttons || []).map(b => h("span", {}, b.label));
  const typeLabel = (TYPES.find(([v]) => v === (a.activity_type ?? 0)) || TYPES[0])[1];
  return h("div", { class: "card" }, h("div", { class: "label" }, title),
    h("div", { class: "preview" }, imgEl,
      h("div", { class: "lines" },
        h("div", { class: "muted small" }, `${typeLabel} <your app name>`),
        a.details ? h("div", {}, h("strong", {}, a.details)) : null,
        a.state ? h("div", {}, a.state) : null,
        a.start ? h("div", { class: "muted small" }, "elapsed timer shown") : null,
        btns.length ? h("div", { class: "pbtns" }, btns) : null)));
}

// ------------------------------------------------------------------ views
function dashboardView() {
  const root = h("div", { class: "stack" });
  const toggle = h("button", { class: "toggle", "aria-pressed": "false", id: "presence-toggle" }, h("span", { class: "knob" }), h("span", { class: "tlabel" }, "…"));
  toggle.addEventListener("click", () => withBusy(toggle, async () => {
    const on = store.state.presence.enabled;
    resultToast(on ? await api.disable() : await api.enable(), on ? "Presence turned off" : "Presence is live");
  }));
  const statusLine = h("div", { class: "muted", id: "status-line" });
  const errorBox = h("div", { class: "field-error", hidden: true, id: "error-line" });
  const applyBtn = h("button", { class: "primary", hidden: true, onclick: () => withBusy(applyBtn, async () => resultToast(await api.applyPresence(), "Changes applied")) }, "Apply saved changes");
  const hero = h("div", { class: "card hero" },
    h("div", {}, h("h1", {}, "Rich Presence"), statusLine, errorBox, applyBtn), toggle);
  const cards = h("div", { class: "grid" });
  const previewSlot = h("div");
  const events = h("ul", { class: "list events" });
  const setupHint = h("div", { class: "card", hidden: true },
    h("h2", {}, "Finish setup"), h("p", {}, "Add your Discord Application ID and an activity to get started."),
    h("a", { href: "#/setup", class: "btn" }, "Open setup wizard"));
  root.append(setupHint, hero, cards, previewSlot, h("div", { class: "card" }, h("h2", {}, "Recent events"), events));

  function card(label, value, extra) {
    return h("div", { class: "card" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), extra || null);
  }

  function update() {
    const s = store.state;
    if (!s) return;
    const p = s.presence, d = s.discord;
    toggle.setAttribute("aria-pressed", p.enabled ? "true" : "false");
    toggle.querySelector(".tlabel").textContent = p.enabled ? "Presence ON" : "Presence OFF";
    statusLine.textContent = describe(s);
    const err = d.last_error;
    errorBox.hidden = !err;
    errorBox.textContent = err ? `${err.message} (${err.code})` : "";
    applyBtn.hidden = !(p.enabled && !p.applied && p.connection_state === "CONNECTED");
    setupHint.hidden = s.configuration.configured || s.presence.enabled;
    const preset = store.presets.find(x => x.id === p.active_preset_id);
    cards.replaceChildren(
      card("Discord connection", d.rpc_connected ? "Connected" : "Not connected",
        h("div", { class: "muted small" }, `State: ${p.connection_state}`)),
      card("Active preset", preset ? preset.name : "Custom activity",
        s.auto_switch.enabled ? h("div", { class: "muted small" }, "Auto-switch by process is on") : null),
      card("Last successful update", fmtAgo(p.last_successful_update),
        h("div", {}, p.enabled ? (p.applied ? h("span", { class: "badge ok" }, "✓ saved settings applied") : h("span", { class: "badge warn" }, "! saved changes not applied")) : null)),
      card("Backend", `v${s.application.version}`,
        h("div", { class: "muted small" }, `${s.server.websocket_connected_clients} live client(s) · ${s.server.lan_active ? "LAN on" : "localhost only"}`)),
    );
    previewSlot.replaceChildren(p.current_activity ? preview(p.current_activity, "Published on Discord") :
      preview(store.config && store.config.presence, "Saved activity (not published)"));
    events.replaceChildren(...(s.recent_events || []).slice().reverse().slice(0, 12).map(e =>
      h("li", {}, h("span", {}, h("span", { class: "t" }, fmtTime(e.at)), `${e.success ? "✓" : "✕"} ${e.message}`),
        h("span", { class: "muted small" }, e.source))));
    if (!s.recent_events || !s.recent_events.length) events.replaceChildren(h("li", { class: "muted" }, "No events yet."));
  }
  return { el: root, update };
}

function appIcon(p) {
  const fallback = h("span", { class: "app-letter", "aria-hidden": "true" }, (p.name.trim()[0] || "?").toUpperCase());
  if (!p.icon) return h("div", { class: "app-icon" }, fallback);
  const img = h("img", { src: p.icon, alt: "", loading: "lazy" });
  img.addEventListener("error", () => img.replaceWith(fallback));
  return h("div", { class: "app-icon" }, img);
}

function appsView() {
  const grid = h("div", { class: "apps", role: "group", "aria-label": "Apps" });
  const root = h("div", { class: "card" },
    h("h1", {}, "App"),
    h("p", { class: "muted small" }, "Click an app to show it on Discord. Only one app is on at a time; click it again to turn it off."),
    grid);

  function update() {
    const s = store.state;
    const onId = s && s.presence.enabled ? s.presence.active_app_id : null;
    if (!store.presets.length) {
      grid.replaceChildren(h("p", { class: "empty" }, "No apps yet. ",
        h("a", { href: "#/presets" }, "Create a preset"), " — every preset appears here as an app."));
      return;
    }
    grid.replaceChildren(...store.presets.map(p => {
      const on = p.id === onId;
      const btn = h("button", { class: "app", "aria-pressed": on ? "true" : "false", title: on ? `Turn ${p.name} off` : `Show ${p.name} on Discord` },
        appIcon(p), h("span", { class: "app-name" }, p.name), h("span", { class: "app-state" }, on ? "ON" : "OFF"));
      btn.addEventListener("click", () => withBusy(btn, async () =>
        resultToast(await api.toggleApp(p.id), on ? `${p.name} turned off` : `${p.name} is live`)));
      return btn;
    }));
  }
  return { el: root, update };
}

function activityView() {
  const form = activityForm(store.config && store.config.presence, "edit");
  const cid = h("input", { type: "text", id: "edit-client-id", inputmode: "numeric", placeholder: "default Application ID" });
  const cidErr = h("div", { class: "field-error", hidden: true });
  const remote = h("div", { class: "banner", hidden: true },
    "The saved activity was changed elsewhere. ", h("button", { onclick: () => load() }, "Load latest"));
  const save = h("button", {}, "Save");
  const saveApply = h("button", { class: "primary" }, "Save & apply");
  const reset = h("button", { type: "button", onclick: () => load() }, "Reset");
  const el = h("form", { class: "card", novalidate: true, onsubmit: (e) => e.preventDefault() },
    h("h1", {}, "Activity editor"),
    h("p", { class: "muted small" }, "Save stores the activity. Save & apply also publishes it to Discord when presence is on."),
    remote,
    h("div", { class: "row" }, h("label", { for: "edit-client-id" }, "Application ID override"),
      h("div", {}, cid, h("div", { class: "hint" }, "Optional. Leave empty to use the default ID from setup."), cidErr)),
    form.el,
    h("div", { class: "actions" }, saveApply, save, reset));

  let cidInitial = "";
  function load() {
    form.set(store.config && store.config.presence);
    cid.value = (store.config && store.config.presence_client_id) || "";
    cidInitial = cid.value;
    form.setErrors([]); cidErr.hidden = true; remote.hidden = true;
  }
  load();
  const dirty = () => form.dirty() || cid.value.trim() !== cidInitial;

  async function submit(apply, button) {
    await withBusy(button, async () => {
      form.setErrors([]); cidErr.hidden = true;
      try {
        const body = await api.updatePresence(form.read(), apply, cid.value.trim());
        resultToast(body, apply ? "Saved and applied" : "Saved");
        cidInitial = cid.value.trim();
        form.set(form.read());
      } catch (e) {
        if (e instanceof ApiError && e.details.length) {
          form.setErrors(e.details);
          const c = e.details.find(d => /client_id/.test(d.field));
          if (c) { cidErr.hidden = false; cidErr.textContent = c.message; }
          toast("Please fix the highlighted fields", "err");
        } else throw e;
      }
    });
  }
  save.addEventListener("click", () => submit(false, save));
  saveApply.addEventListener("click", () => submit(true, saveApply));
  return {
    el, update(kind) {
      if (kind !== "config") return;
      if (dirty()) remote.hidden = false; else load();
    },
  };
}

function presetsView() {
  const root = h("div", { class: "stack" });
  const list = h("ul", { class: "list" });
  const editorSlot = h("div");
  const newBtn = h("button", { class: "primary", onclick: () => openEditor(null) }, "+ New preset");
  root.append(h("div", { class: "card" },
    h("div", { class: "hero" }, h("h1", {}, "Presets"), newBtn),
    h("p", { class: "muted small" }, "Activating a preset copies it into the current activity (and publishes it if presence is on)."),
    list), editorSlot);

  function openEditor(preset) {
    const p = preset || { name: "", client_id: "", process_names: [], icon: "", activity: (store.config && store.config.presence) || {} };
    const name = h("input", { type: "text", id: "preset-name", maxlength: 64, value: p.name });
    const cid = h("input", { type: "text", id: "preset-cid", inputmode: "numeric", value: p.client_id || "" });
    const icon = h("input", { type: "text", id: "preset-icon", maxlength: 512, value: p.icon || "", placeholder: "https://…/logo.png" });
    const procs = h("input", { type: "text", id: "preset-procs", value: (p.process_names || []).join(", "), placeholder: "Code.exe, Figma.exe" });
    const form = activityForm(p.activity, "preset");
    const err = h("div", { class: "field-error", hidden: true });
    const saveBtn = h("button", { class: "primary" }, preset ? "Save preset" : "Create preset");
    const el = h("form", { class: "card", novalidate: true, onsubmit: (e) => e.preventDefault() },
      h("h2", {}, preset ? `Edit “${preset.name}”` : "New preset"),
      h("div", { class: "row" }, h("label", { for: "preset-name" }, "Name"), h("div", {}, name)),
      h("div", { class: "row" }, h("label", { for: "preset-cid" }, "Application ID (optional)"),
        h("div", {}, cid, h("div", { class: "hint" }, "Use a different Discord application for this preset; empty = default."))),
      h("div", { class: "row" }, h("label", { for: "preset-icon" }, "App logo (optional)"),
        h("div", {}, icon, h("div", { class: "hint" }, "https:// image URL shown on the card in the App section."))),
      h("div", { class: "row" }, h("label", { for: "preset-procs" }, "Auto-switch processes"),
        h("div", {}, procs, h("div", { class: "hint" }, "Comma-separated executable names. Used when auto-switch is enabled in Settings."))),
      form.el, err,
      h("div", { class: "actions" }, saveBtn, h("button", { type: "button", onclick: () => editorSlot.replaceChildren() }, "Cancel")));
    saveBtn.addEventListener("click", () => withBusy(saveBtn, async () => {
      form.setErrors([]); err.hidden = true;
      const body = {
        name: name.value.trim(), client_id: cid.value.trim(), icon: icon.value.trim(), activity: form.read(),
        process_names: procs.value.split(",").map(s => s.trim()).filter(Boolean),
      };
      try {
        if (preset) await api.updatePreset(preset.id, body); else await api.createPreset(body);
        toast(preset ? "Preset saved" : "Preset created", "ok");
        editorSlot.replaceChildren();
      } catch (e) {
        if (e instanceof ApiError && e.details.length) {
          form.setErrors(e.details.map(d => ({ ...d, field: d.field.replace(/^activity\./, "") })));
          const other = e.details.filter(d => !d.field.startsWith("activity"));
          if (other.length) { err.hidden = false; err.textContent = other.map(d => `${d.field}: ${d.message}`).join("; "); }
          toast("Please fix the highlighted fields", "err");
        } else throw e;
      }
    }));
    editorSlot.replaceChildren(el);
    name.focus();
  }

  function update() {
    const activeId = store.state && store.state.presence.active_preset_id;
    if (!store.presets.length) {
      list.replaceChildren(h("li", { class: "empty" }, "No presets yet. Create one to switch activities with one click."));
      return;
    }
    list.replaceChildren(...store.presets.map(p => {
      const act = h("button", { class: "primary" }, "Activate");
      const actOn = h("button", {}, "Activate & turn on");
      act.addEventListener("click", () => withBusy(act, async () => resultToast(await api.activatePreset(p.id, false), "Preset activated")));
      actOn.addEventListener("click", () => withBusy(actOn, async () => resultToast(await api.activatePreset(p.id, true), "Preset is live")));
      const dup = h("button", { onclick: () => withBusy(dup, async () => { await api.duplicatePreset(p.id); toast("Preset duplicated", "ok"); }) }, "Duplicate");
      const del = h("button", { class: "danger" }, "Delete");
      del.addEventListener("click", () => {
        if (!confirm(`Delete preset “${p.name}”? This cannot be undone.`)) return;
        withBusy(del, async () => { await api.deletePreset(p.id); toast("Preset deleted", "ok"); });
      });
      return h("li", {},
        h("div", {}, h("strong", {}, p.name), " ", p.id === activeId ? h("span", { class: "badge ok" }, "✓ active") : null,
          h("div", { class: "muted small" }, [p.activity.details, p.activity.state].filter(Boolean).join(" · ") || "(no text)",
            p.process_names.length ? ` · auto: ${p.process_names.join(", ")}` : "")),
        h("div", { class: "actions" }, act, actOn, h("button", { onclick: () => openEditor(p) }, "Edit"), dup, del));
    }));
  }
  return { el: root, update };
}

function setupView() {
  const STEPS = ["Introduction", "Developer Portal", "Application ID", "Activity", "Test connection", "Finish"];
  let step = 0;
  const cfg = store.config || { application: {}, presence: {} };
  const cid = h("input", { type: "text", id: "wiz-cid", inputmode: "numeric", value: cfg.application.client_id || "", placeholder: "e.g. 1468102806786281596" });
  const cidErr = h("div", { class: "field-error", hidden: true });
  const form = activityForm(cfg.presence, "wiz");
  const testOut = h("div", { class: "muted", role: "status" }, "Not tested yet.");
  const testActivation = h("input", { type: "checkbox", id: "wiz-test" });
  let tested = false;
  const body = h("div");
  const nav = h("div", { class: "actions" });
  const stepper = h("div", { class: "steps" });
  const el = h("div", { class: "card" }, h("h1", {}, "Setup wizard"), stepper, body, nav);

  const pages = [
    () => h("div", {},
      h("p", {}, "Discord-Pro shows a custom Rich Presence (the “Playing …” card) on your Discord profile."),
      h("p", {}, "You need:"),
      h("ul", {}, h("li", {}, "The Discord desktop app running on this computer (the browser version cannot be used)."),
        h("li", {}, "A free “application” in the Discord Developer Portal — its name is what appears after “Playing”."),
        h("li", {}, "In Discord: User Settings → Activity Privacy → “Share my activity” turned on."))),
    () => h("div", {},
      h("ol", { class: "guide" },
        h("li", {}, "Open ", h("a", { href: "https://discord.com/developers/applications", target: "_blank", rel: "noopener noreferrer" }, "discord.com/developers/applications"), " and sign in."),
        h("li", {}, "Click “New Application”, choose the name you want to show, and accept the terms."),
        h("li", {}, "On “General Information”, copy the ", h("strong", {}, "Application ID"), "."),
        h("li", {}, "Optional: under “Rich Presence → Art Assets” upload images. The asset name (e.g. ", h("code", {}, "logo"), ") goes in the image fields. New assets can take a few minutes to appear.")),
      h("p", { class: "muted small" }, "Discord does not offer an API to create applications for you, so this step is manual. Never paste your Discord account token anywhere.")),
    () => h("div", {}, h("div", { class: "row" }, h("label", { for: "wiz-cid" }, "Application ID"), h("div", {}, cid, cidErr))),
    () => h("div", {}, form.el),
    () => h("div", {}, h("p", {}, "Checks that Discord is running and accepts this Application ID (a real RPC handshake)."),
      h("button", { class: "primary", id: "wiz-run-test", onclick: runTest }, "Test connection"), " ", testOut),
    () => h("div", {}, h("p", {}, tested ? "Connection verified." : "Connection not verified — you can still save and test later."),
      h("label", { class: "check" }, testActivation, "Turn the presence on now to try it")),
  ];

  async function runTest(ev) {
    await withBusy(ev.currentTarget, async () => {
      testOut.textContent = "Testing…";
      const r = await api.setupTest(cid.value.trim());
      if (r.result.success) { tested = true; testOut.textContent = "✓ Connected to Discord."; }
      else { tested = false; testOut.textContent = `✕ ${r.result.error.message}`; }
    });
  }

  async function validateStep() {
    if (step === 2) {
      const r = await api.setupValidate(cid.value.trim(), null);
      const e = r.errors.find(x => x.field === "client_id");
      cidErr.hidden = !e; cidErr.textContent = e ? e.message : "";
      return !e;
    }
    if (step === 3) {
      const r = await api.setupValidate(cid.value.trim(), form.read());
      form.setErrors(r.errors.filter(x => x.field.startsWith("activity")).map(x =>
        ({ ...x, field: x.field === "activity" ? "details" : x.field })));
      return r.valid;
    }
    return true;
  }

  function render() {
    stepper.replaceChildren(...STEPS.map((s, i) => h("span", { "aria-current": i === step ? "step" : null }, `${i + 1}. ${s}`)));
    body.replaceChildren(pages[step]());
    const back = h("button", { disabled: step === 0, onclick: () => { step--; render(); } }, "Back");
    const next = h("button", { class: "primary" }, step === STEPS.length - 1 ? "Save & finish" : "Next");
    next.addEventListener("click", () => withBusy(next, async () => {
      if (step < STEPS.length - 1) {
        if (await validateStep()) { step++; render(); }
        return;
      }
      const r = await api.setupComplete(cid.value.trim(), form.read(), testActivation.checked);
      resultToast(r, "Setup saved");
      if (r.result.success || r.result.pending) location.hash = "#/dashboard";
    }));
    nav.replaceChildren(back, next);
  }
  render();
  return { el, update() {} };
}

function settingsView() {
  const root = h("div", { class: "stack" });
  const status = h("div");
  const form = h("form", { class: "card", novalidate: true, onsubmit: (e) => e.preventDefault() });
  const diag = h("pre", { class: "card mono small", hidden: true });
  root.append(status, form, h("div", { class: "card" }, h("h2", {}, "Diagnostics"),
    h("div", { class: "actions" },
      h("button", { onclick: async () => { try { diag.textContent = JSON.stringify(await api.diagnostics(), null, 2); diag.hidden = false; } catch (e) { handleError(e); } } }, "Show diagnostics"),
      h("button", { onclick: async () => { await api.logout(); showLogin(); } }, "Sign out of this browser"))), diag);

  let initial = "";
  function build() {
    const c = store.config;
    if (!c) return;
    const lan = h("input", { type: "checkbox", id: "set-lan", checked: c.server.allow_lan });
    const port = h("input", { type: "number", id: "set-port", min: 1024, max: 65535, value: c.server.port });
    const startOn = h("input", { type: "checkbox", id: "set-start", checked: c.application.start_with_presence_enabled });
    const auto = h("input", { type: "checkbox", id: "set-auto", checked: c.auto_switch.enabled });
    const poll = h("input", { type: "number", id: "set-poll", min: 1, max: 60, step: "1", value: c.auto_switch.poll_interval });
    const attempts = h("input", { type: "number", id: "set-attempts", min: 1, max: 1000, value: c.rpc.reconnect_max_attempts });
    const maxDelay = h("input", { type: "number", id: "set-maxdelay", min: 1, max: 3600, value: c.rpc.reconnect_max_delay });
    const save = h("button", { class: "primary" }, "Save settings");
    const read = () => ({
      server: { allow_lan: lan.checked, port: Number(port.value) },
      application: { start_with_presence_enabled: startOn.checked },
      auto_switch: { enabled: auto.checked, poll_interval: Number(poll.value) },
      rpc: { reconnect_max_attempts: Number(attempts.value), reconnect_max_delay: Number(maxDelay.value) },
    });
    save.addEventListener("click", () => withBusy(save, async () => {
      const patch = read();
      if (patch.server.allow_lan && !store.config.server.allow_lan &&
          !confirm("Allow devices on your local network to open this dashboard? They will need the access code shown in the desktop app. Only enable this on a trusted network.")) return;
      store.config = await api.updateConfig(patch);
      initial = JSON.stringify(read());
      toast("Settings saved", "ok");
    }));
    const r = (id, label, control, hint) => h("div", { class: "row" }, h("label", { for: id }, label), h("div", {}, control, hint ? h("div", { class: "hint" }, hint) : null));
    form.replaceChildren(h("h1", {}, "Settings"),
      h("fieldset", {}, h("legend", {}, "Presence"),
        r("set-start", "Start enabled", h("label", { class: "check" }, startOn, "Turn presence on when the backend starts")),
        r("set-auto", "Auto-switch", h("label", { class: "check" }, auto, "Activate presets when their programs are running"),
          "Uses the process names configured on each preset."),
        r("set-poll", "Check every (s)", poll)),
      h("fieldset", {}, h("legend", {}, "Reconnect"),
        r("set-attempts", "Max attempts", attempts, "After this many failed attempts the backend stops retrying until you toggle again."),
        r("set-maxdelay", "Max delay (s)", maxDelay)),
      h("fieldset", {}, h("legend", {}, "Network"),
        r("set-lan", "LAN access", h("label", { class: "check" }, lan, "Allow other devices on this Wi-Fi"),
          "Off by default. Requires a backend restart. Other devices sign in with the access code from the desktop app."),
        r("set-port", "Port", port, "Requires a backend restart.")),
      h("div", { class: "actions" }, save));
    initial = JSON.stringify(read());
    form._dirty = () => JSON.stringify(read()) !== initial;
  }

  function update(kind) {
    const s = store.state;
    if (s) {
      status.replaceChildren(h("div", { class: "card" }, h("h2", {}, "Server"),
        h("div", { class: "grid" },
          h("div", {}, h("div", { class: "label muted small" }, "Address"), h("div", { class: "mono" }, `${location.host}`)),
          h("div", {}, h("div", { class: "label muted small" }, "Network access"), s.server.lan_active ? "LAN (authenticated)" : "This computer only"),
          h("div", {}, h("div", { class: "label muted small" }, "Live clients"), String(s.server.websocket_connected_clients))),
        s.server.restart_required ? h("p", { class: "badge warn" }, "Restart the backend to apply network changes") : null));
    }
    if (kind === "config" || !form.firstChild) { if (!form._dirty || !form._dirty()) build(); }
  }
  return { el: root, update };
}

const ROUTES = { dashboard: dashboardView, apps: appsView, activity: activityView, presets: presetsView, setup: setupView, settings: settingsView };

function route() {
  if (!store.authed) return;
  const name = (location.hash.replace(/^#\//, "") || "dashboard").split("?")[0];
  const factory = ROUTES[name] || ROUTES.dashboard;
  for (const a of document.querySelectorAll("#nav a")) a.setAttribute("aria-current", a.dataset.route === name ? "page" : "false");
  currentView = factory();
  const main = document.getElementById("view");
  main.replaceChildren(currentView.el);
  currentView.update("state");
}

listeners.add((kind) => {
  renderPill();
  renderBanner();
  if (currentView) currentView.update(kind);
});

// ----------------------------------------------------------- live events
function onEvent(msg) {
  switch (msg.type) {
    case "state.snapshot":
      store.config = msg.payload.config;
      store.presets = msg.payload.presets;
      setState(msg.payload.state, { force: true });
      emit("config"); emit("presets");
      break;
    case "state.updated": setState(msg.payload); break;
    case "config.updated": store.config = msg.payload; emit("config"); break;
    case "presets.updated": store.presets = msg.payload; emit("presets"); break;
    default: break;
  }
}

function startLive() {
  if (socket) socket.close();
  socket = new LiveSocket({
    onEvent,
    onStatus: (status) => { store.ws = status; renderPill(); renderBanner(); },
    onAuthLost: () => showLogin("Your session ended (the backend may have restarted). Please sign in again."),
    checkAuth: async () => {
      try { return (await api.session()).authenticated; } catch (e) { return true; } // offline: keep retrying
    },
  });
  socket.connect();
}

// ------------------------------------------------------------------ auth
function showLogin(message) {
  store.authed = false;
  if (socket) { socket.close(); socket = null; }
  document.getElementById("nav").hidden = true;
  const code = h("input", { type: "password", id: "login-code", autocomplete: "one-time-code", placeholder: "XXXX-XXXX-XXXX" });
  const err = h("div", { class: "field-error", hidden: !message }, message || "");
  const btn = h("button", { class: "primary", type: "submit" }, "Sign in");
  const form = h("form", { class: "card login" },
    h("h1", {}, "Sign in"),
    h("p", { class: "muted" }, "Open the dashboard from the Discord-Pro desktop app (tray → Open dashboard) to sign in automatically, or enter the access code shown in the desktop app’s Settings."),
    h("div", { class: "row" }, h("label", { for: "login-code" }, "Access code"), h("div", {}, code, err)),
    h("div", { class: "actions" }, btn));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    btn.disabled = true;
    try {
      await api.login(code.value);
      await boot();
    } catch (ex) {
      err.hidden = false;
      err.textContent = ex.message;
    } finally { btn.disabled = false; }
  });
  document.getElementById("view").replaceChildren(form);
  renderPill();
  code.focus();
}

async function boot() {
  const params = new URLSearchParams(location.search);
  let session;
  try { session = await api.session(); } catch (e) { session = { authenticated: false }; }
  if (!session.authenticated) {
    showLogin(params.get("login") === "expired" ? "That sign-in link has expired. Open the dashboard again from the desktop app." : "");
    return;
  }
  if (params.has("login")) history.replaceState(null, "", "/" + location.hash);
  store.authed = true;
  document.getElementById("nav").hidden = false;
  try {
    const [state, config, presets] = await Promise.all([api.state(), api.config(), api.presets()]);
    store.config = config; store.presets = presets;
    setState(state, { force: true });
  } catch (e) { handleError(e); }
  startLive();
  route();
}

window.addEventListener("hashchange", route);
setInterval(() => { if (currentView && store.state) renderPill(); }, 1000);
boot();
