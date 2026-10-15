# Changelog

## Unreleased

### Added
- **App** section (GUI tab and web dashboard page, next to Dashboard): every preset is shown as an app card with its logo. Clicking a card turns that app ON (replacing any other app) or OFF. Not tied to running processes.
- `POST /api/v1/apps/{id}/toggle` and WebSocket command `app.toggle`.
- Optional `icon` (https image URL) on presets; `active_app_id` in `config.json` and in the canonical state. The app that was ON is turned back on when the backend starts.

## 2.0.0 – Integrated Rich Presence manager

### Added
- Local backend (FastAPI/uvicorn) owning a single Discord RPC connection: state machine (`DISCONNECTED/CONNECTING/CONNECTED/RECONNECTING/ERROR/STOPPING`), bounded exponential backoff with jitter, crash detection, update throttling/coalescing, permanent-vs-transient error handling.
- Versioned REST API (`/api/v1`) and WebSocket event protocol sharing one canonical state.
- Desktop GUI (PySide6): dashboard with ON/OFF toggle, activity editor (save vs apply), presets, setup wizard with real connection test, settings, diagnostics, system tray with hide-on-close.
- Web dashboard served by the backend: same features, real-time sync, responsive layout, access-code login for LAN devices.
- Process auto-switch: presets can be activated while a program runs (replaces the three standalone agents).
- Validated, atomically written configuration with migration and partial recovery; preset store.
- Security: token + HttpOnly session auth, DNS-rebinding/CSRF/origin protection, opt-in LAN, rate limits, log redaction.
- CLI: `python -m discord_pro [gui|backend|start|stop|status|on|off|open-dashboard]`.
- 153 automated tests (unit, integration, process E2E, browser E2E, GUI) incl. a Discord IPC simulator.
- PyInstaller spec (one-folder build, GUI + `--backend` mode in one executable), Windows helper scripts.
- Docs: README, ARCHITECTURE, SECURITY, TESTING, AUDIT.

### Changed
- Original agents moved to `legacy/` unchanged; their activities are available as example presets.

### Removed
- Committed build artefacts (`build/`, `dist/*.exe`) from version control.

### Known issues
- Not yet verified against a real Discord client, a real OS tray, or a Windows build (see docs/TESTING.md).
