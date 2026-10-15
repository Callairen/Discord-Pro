# Architecture

## Components

```text
         Discord desktop client
                  │  local IPC pipe (discord-ipc-N), Discord RPC protocol
        ┌─────────┴──────────┐
        │ rpc.py             │  PypresenceAdapter on a dedicated thread + event loop
        │ manager.py         │  RpcManager: the ONLY owner of an RPC connection
        ├────────────────────┤
        │ service.py         │  PresenceService: every command (REST, WS, auto-switch)
        │ state.py           │  StateManager (canonical state) + EventBus
        │ config.py presets.py│ validated JSON persistence (atomic writes)
        ├────────────────────┤
        │ app.py             │  FastAPI: /api/v1 REST, /api/v1/ws, static dashboard
        │ security.py        │  token / session auth, Host/Origin/peer policy, limits
        └───────┬─────┬──────┘
       REST+WS  │     │  REST+WS (cookie session)
        Desktop GUI   Web dashboard
        (PySide6)     (vanilla JS, served by the backend)
```

| Component | Responsibility |
|---|---|
| `backend/rpc.py` | Wraps `pypresence.AioPresence`. All IPC I/O runs on one thread with a private asyncio loop (a `ProactorEventLoop` on Windows, required for named pipes). Exceptions are classified as `transient`, `permanent` or `payload`. Closes the pipe itself because pypresence's `close()` also closes the event loop. |
| `backend/manager.py` | Connection state machine and the single adapter owner (so competing RPC connections cannot exist). Commands set a *desired* state; one worker task reconciles. |
| `backend/service.py` | Business operations: enable, disable, update/apply activity, presets, config, setup test, process auto-switch. Serialized by one `asyncio.Lock`. |
| `backend/state.py` | Canonical state dict with a monotonically increasing `revision`, plus a bounded fan-out bus for WebSocket clients. |
| `backend/app.py` | REST and WebSocket adapters over the service. Neither keeps state of its own. |
| `gui/` | Desktop client: async REST via `QNetworkAccessManager`, WebSocket on a `QThread` whose events reach widgets through Qt signals (never touching widgets from another thread). |
| `web/` | Browser client: `api.js` (REST + reconnecting WebSocket), `app.js` (views). No build step, no browser storage. |
| `local_client.py` | Discovery (`instance.json` + `auth.json`), backend launcher, CLI helper. |

**Technology decisions.** The original repository was three standalone scripts (Python, pypresence, psutil, PyInstaller), with no existing server, GUI or web framework to keep. Python was kept, along with pypresence (Discord's RPC protocol is undocumented for third parties; pypresence is the maintained client the original code already used), psutil (process detection) and PyInstaller. FastAPI/uvicorn provide REST + WebSocket, and PySide6 provides the desktop UI and tray. The dashboard is plain ES modules served as static files: this avoids adding Node, a bundler and a second language to a small project.

## Process lifecycle

1. The GUI starts (`python -m discord_pro` or `DiscordPro.exe`). A `QLocalServer` keeps the GUI single-instance (a second launch just raises the existing window).
2. The GUI reads `instance.json` and probes `/api/v1/health`. If no backend answers, it spawns one as a detached process (`python -m discord_pro.backend` / `DiscordPro.exe --backend`) and waits up to 20 s.
3. The backend takes an exclusive OS file lock (`backend.lock`). A second backend exits with code 3. The lock is released by the OS if the process dies.
4. It loads and validates configuration, starts the RPC manager and auto-switch watcher, serves HTTP, then writes `instance.json` (pid, port).
5. GUI and browsers connect over REST + WebSocket. Closing the window hides it (tray); closing a tab does nothing to the backend.
6. Explicit stop (`POST /api/v1/shutdown`, tray "Exit and stop backend", `python -m discord_pro stop`, SIGTERM/Ctrl+C): uvicorn shuts down gracefully → lifespan clears the activity and closes the pipe → `instance.json` is removed → lock released.

This is a supervised background process, not an OS service. There is no autostart at login.

## State ownership and data flow

The backend is the only authority. Example: the browser changes the activity.

```text
browser PUT /api/v1/presence ─► service.update_activity ─► config.json (atomic)
                                   │                         └─► bus: config.updated
                                   └─► manager.apply(Desired) ─► adapter.set_activity ─► Discord
                                                 └─► on_change ─► StateManager.update ─► bus: state.updated
bus ─► every WebSocket client (GUI, other browsers) ─► UI refresh (no polling)
```

Stale responses: every state carries `revision` (plus `application.started_at` to detect a restarted backend). Clients ignore a state older than the one they show; a `state.snapshot` after (re)connect always replaces local state.

### Canonical state (`GET /api/v1/state`)

```text
revision                            int, increments on every change
application.running/version/started_at
presence.enabled                    user's desired ON/OFF (intent)
presence.enabled_by                 "desktop" | "browser" | "websocket" | "auto-switch" | "startup" | null
presence.connection_state           DISCONNECTED | CONNECTING | CONNECTED | RECONNECTING | ERROR | STOPPING
presence.current_activity           payload Discord accepted (null if nothing published)
presence.last_successful_update     epoch seconds
presence.active_preset_id
presence.applied                    false when saved settings differ from what is published
discord.rpc_connected / connected_client_id / reconnect_attempt / next_retry_in
discord.last_error                  {code, message, kind, at} | null
server.host/port/allow_lan          configured values
server.lan_active                   policy currently enforced (changes need a restart)
server.restart_required, websocket_connected_clients
configuration.configured / configuration_valid / problems[] / warnings[]
auto_switch.enabled / matched_preset_id
recent_events[]                     last 30 {at, kind, message, success, source}
```

Intent and connection are separate: `enabled=true` + `RECONNECTING` means "the user wants it on, Discord is unavailable, retrying", which is different from `enabled=false` + `DISCONNECTED` (turned off on purpose) and from `ERROR` (stopped retrying, needs a user action).

## Discord RPC integration

- Mechanism: the documented-by-practice local RPC/IPC used by Rich Presence SDKs (`SET_ACTIVITY` over `discord-ipc-N`). No bot token, OAuth token or user token is used; only the public Application ID.
- Handshake errors: `Invalid Client ID` → permanent `invalid_client_id` (no retry). Pipe missing → transient `discord_not_found`.
- **ON:** validate config → connect if needed → publish → state `CONNECTED` → broadcast. Identical repeated requests do not reconnect or resend.
- **OFF:** `clear` over the pipe, then close. If `clear` fails because the pipe already broke, the result says `cleared: "disconnect"`, because Discord removes an RPC client's activity when its connection closes. If nothing was ever published: `cleared: "none"`.
- **Disconnect detection:** the RPC loop runs continuously, so the transport sees EOF when Discord quits; the manager checks every 2 s and moves to `RECONNECTING`, then republishes after reconnecting.
- **Backoff:** `delay = min(max, base·2^(n-1)) · U(0.8, 1.2)`; defaults base 2 s, max 60 s, 20 attempts, then `ERROR` (`reconnect_exhausted`) until the next user command.
- **Rate limit:** at least `min_update_interval` (4 s) between `SET_ACTIVITY` calls; intermediate edits are coalesced into the newest one.
- **Payload validation** (`activity.py`): details/state/tooltips 2–128 chars, images = asset key or http(s) URL ≤ 256, ≤ 2 buttons, label 1–32, URL http(s) 1–512 without credentials, unknown fields rejected. Activity types: Playing, Listening, Watching, Competing.

## REST API (`/api/v1`)

Auth column: **T** = API token (`Authorization: Bearer`), **S** = browser session cookie (+ `X-Discord-Pro: 1` header for writes). Errors: `{"error": {"code", "message", "details?"}}`. Presence commands return `{"result": {success, pending, error, data?}, "state": {...}}` with 200 on success, 422 for configuration/validation/permanent errors, 409 for conflicts, 503 when Discord is unavailable (`pending: true` while retrying).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | `{status, version, running, uptime}` |
| GET | `/state` | T/S | canonical state |
| GET / PUT | `/config` | T/S | non-secret config / deep-merge patch (validated, atomic) |
| POST | `/presence/enable`, `/presence/disable`, `/presence/apply` | T/S | ON, OFF, publish saved activity |
| PUT | `/presence` | T/S | `{activity, apply, client_id?}` save (and optionally apply) |
| GET / POST | `/presets` | T/S | list / create |
| PUT / DELETE | `/presets/{id}` | T/S | update / delete (404 `preset_not_found`) |
| POST | `/presets/{id}/activate` | T/S | `{enable?}`; validated before the active config is touched |
| POST | `/presets/{id}/duplicate` | T/S | copy |
| POST | `/setup/validate` | T/S | `{client_id, activity?}` → `{valid, errors[]}` (no side effects) |
| POST | `/setup/test-connection` | T/S | real handshake; refuses (409) while presence is on with a different ID |
| POST | `/setup/complete` | T/S | save ID + activity, mark configured, optional `test_activation` |
| GET | `/diagnostics` | T/S | RPC status, recent errors, warnings; host details only for loopback clients |
| GET | `/auth/session` | none | `{authenticated}` for the cookie |
| POST | `/auth/login` | none | `{access_code}` → session cookie (5 attempts / 5 min / IP) |
| POST | `/auth/logout` | none | revoke cookie |
| POST | `/auth/login-link` | T only | one-time `/auth/redeem?code=…` (120 s, single use) |
| GET / POST | `/auth/access-code`, `/auth/access-code/rotate` | T only | show / rotate (rotation signs out all browsers) |
| POST | `/shutdown` | T only | graceful stop |

OpenAPI schema: `/api/v1/openapi.json`.

## WebSocket protocol (`/api/v1/ws`, protocol 1)

Auth: same token or cookie; `Origin` (if sent) must match `Host`. Rejections: `4403` (network/origin policy), `4401` (not authenticated; accepted then closed so browsers can read the code), `4408` (client too slow, its queue overflowed; reconnect and resync). Max message 64 KiB; uvicorn pings every 20 s and drops dead peers.

Every message: `{"type": str, "request_id": str|null, "payload": object}`.

Server → client:

| type | payload |
|---|---|
| `hello` | `{protocol: 1, version}`, first message |
| `state.snapshot` | `{state, config, presets}`, sent on connect and on `state.get` |
| `state.updated` | full canonical state (see above) |
| `config.updated` | full non-secret config |
| `presets.updated` | full preset list |
| `operation.result` | `{success, pending, error: {code, message, details?} \| null, message?, data?, duplicate?}`, correlated by `request_id` |
| `pong` | `{}` |

Client → server (`request_id` optional, 1–64 chars; a repeated `request_id` returns the original result with `duplicate: true` and is not executed again):

| type | payload |
|---|---|
| `ping`, `state.get` | `{}` |
| `presence.enable`, `presence.disable`, `presence.apply` | `{}` |
| `presence.update` | `{activity, apply?, client_id?}` |
| `preset.create` | `{preset: {name, client_id?, process_names?, activity}}` |
| `preset.update` | `{id, preset}` |
| `preset.delete`, `preset.duplicate` | `{id}` |
| `preset.activate` | `{id, enable?}` |
| `config.update` | `{patch}` |

Invalid input produces `operation.result` with codes `invalid_json`, `invalid_message`, `invalid_request_id`, `invalid_payload`, `unknown_command`, `validation_error`, `message_too_large`, `rate_limited`; the connection stays open. Commands run through the same `PresenceService` methods as REST. The GUI and dashboard send commands over REST and use the socket for events; WebSocket commands are available to other clients and covered by tests.

## Persistence

- `config.json`: versioned (`schema_version: 1`), Pydantic-validated, written atomically (temp file + fsync + replace). Migration from a flat v0 layout. Malformed file → backup + defaults; partially invalid → only invalid sections reset (backup kept).
- `presets.json`: invalid entries are skipped and preserved in `presets.invalid.json`.
- `auth.json`: API token + access code, `0600` on POSIX.
- Browser sessions and one-time login codes are in memory only: a backend restart signs browsers out.

## Failure and recovery

| Failure | Behaviour |
|---|---|
| Discord not running | `RECONNECTING` with backoff; publishes automatically once Discord starts. |
| Discord quits / crashes | detected within ~2 s, reconnects, republishes. |
| Invalid Application ID | `ERROR` immediately, no retries. |
| Activity rejected by Discord | connection kept, `last_error` set, previous activity remains. |
| Retries exhausted | `ERROR` (`reconnect_exhausted`) until the user toggles again. |
| Config write fails | 500 `config_write_failed`; in-memory config unchanged. |
| WebSocket client vanishes | removed; others unaffected. |
| Backend restarts | GUI rediscovers port/token and resyncs; browsers are asked to sign in again. |
| Backend killed hard | lock released by the OS; stale `instance.json` is ignored (health check fails) and replaced on next start. Discord drops the activity when the pipe closes. |
