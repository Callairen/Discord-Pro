# Testing

## Setup

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium     # browser E2E only
```

| Suite | Command | What it covers |
|---|---|---|
| Unit | `pytest tests/unit` | payload validation, config load/migration/partial reset/atomic write, presets, backoff, RPC state machine (fake adapter) |
| Integration | `pytest tests/integration` | REST API, WebSocket protocol, auth/CSRF/Host/LAN policy, rate limits, auto-switch, **real pypresence adapter against a Discord IPC simulator** |
| E2E (process) | `pytest tests/e2e/test_backend_process.py` | real `python -m discord_pro.backend` process, IPC simulator, duplicate-instance lock, shutdown, restart persistence, CLI, log redaction |
| E2E (browser) | `pytest tests/e2e/test_web_dashboard.py` | Chromium via Playwright: wizard, toggle, multi-client sync, validation display, presets, access-code login, mobile layout, backend restart |
| GUI | `QT_QPA_PLATFORM=offscreen pytest tests/gui` | PySide6 window against a real backend process: toggle, remote updates, unsaved-edit protection, validation, presets, hide-on-close, restore, reconnect, settings, wizard, exit & stop |

Environment variables: `DISCORD_PRO_CHROMIUM` (Chromium path), `DISCORD_PRO_SCREENSHOTS` (where E2E/GUI screenshots go; default `docs/screenshots`).

**The IPC simulator is not Discord.** `tests/fakes.py::FakeDiscordIPC` implements Discord's IPC framing (handshake, `SET_ACTIVITY`, close, error frames, drop-activity-on-disconnect) on a Unix socket, so the real pypresence code path runs end to end. It proves that our integration works against the protocol as implemented by the simulator, not that the real Discord client accepts every payload. The manual checklist below covers that.

## Results (executed 2026-10-15, Linux container, Python 3.13.16, PySide6 6.12.0, FastAPI 0.143.1, uvicorn 0.54.0, pypresence 4.6.2, Chromium 141)

`pytest tests` → **153 passed, 0 failed, 0 skipped** (42.8 s). Run 4 times in a row with identical results.

| Suite | Tests | Result |
|---|---|---|
| unit | 52 | PASS |
| integration | 73 | PASS |
| e2e backend process | 9 | PASS |
| e2e browser | 8 | PASS |
| gui (offscreen) | 11 | PASS |

Packaging: `pyinstaller packaging/DiscordPro.spec` built `dist/DiscordPro/` on **Linux**. The frozen executable was run: the GUI (offscreen) spawned its own backend via `--backend` in 1.2 s, the bundled dashboard assets were served (200), setup + enable published to the IPC simulator, the backend and presence survived the GUI exiting, and `DiscordPro status` / `DiscordPro stop` worked (presence cleared, `instance.json` removed). **A Windows `.exe` has not been built or run.**

## Acceptance tests

| ID | Scenario | Evidence | Result |
|---|---|---|---|
| AC-01 | Start backend with valid config | `test_ac01_startup_health_and_instance_file` (real process) | PASS |
| AC-02 | Start without config | `test_missing_config_creates_defaults`, `test_unconfigured_start`; dashboard shows "Finish setup" | PASS |
| AC-03 | Invalid Application ID | `test_setup_validate_reports_errors`, browser `test_wizard_then_toggle`, GUI `test_setup_wizard_flow` | PASS |
| AC-04 | Enable with Discord available | `test_ac04_ac05_enable_disable_over_real_ipc` (simulator) | PASS (simulator); real Discord: **manual** |
| AC-05 | Disable, actual outcome reported | same test (`cleared: "clear"`), `test_disable_when_never_connected_reports_nothing_published` | PASS (simulator) |
| AC-06 | Update from GUI → backend → broadcast | `test_activity_save_apply_and_validation` (GUI) | PASS |
| AC-07 | Update from browser → GUI without refresh | `test_remote_change_reflected_without_refresh` (GUI), `test_realtime_sync_between_clients` (2 browsers) | PASS |
| AC-08 | Enable from two clients | `test_ac08_two_clients_enable_single_connection`: 1 handshake, 1 connection | PASS |
| AC-09 | Close window → hidden, backend up | `test_close_hides_window_backend_keeps_running` (tray reported available) | PASS (logic); real tray: **manual** |
| AC-10 | Restore from tray with current state | same test via `show_window()` (the tray action's slot) | PASS (logic); real tray: **manual** |
| AC-11 | Close browser tab | `test_disconnect_and_reconnect_gets_fresh_snapshot` (backend unaffected) | PASS |
| AC-12 | WebSocket client disconnects | same + `test_slow_subscriber_dropped` | PASS |
| AC-13 | WebSocket reconnect gets fresh snapshot | `test_disconnect_and_reconnect_gets_fresh_snapshot`, GUI `test_backend_restart_gui_reconnects` | PASS |
| AC-14 | Discord terminates unexpectedly | `test_ac14_ac15_discord_restart_recovery`, `test_discord_crash_detected_and_recovered` | PASS (simulator) |
| AC-15 | Discord restored | same tests: reconnects + republishes | PASS (simulator) |
| AC-16 | Restart backend, settings restored | `test_ac16_ac20_shutdown_cleans_up_and_restart_restores`, `test_persistence_across_restart` | PASS |
| AC-17 | Unauthorized LAN access | `test_lan_requires_authentication`, `test_lan_peer_rejected_when_lan_disabled`, `test_dns_rebinding_host_rejected` | PASS |
| AC-18 | Malformed WebSocket messages | `test_malformed_messages_rejected_safely` (10 cases, connection stays usable) | PASS |
| AC-19 | Delete nonexistent preset | `test_preset_errors` (404 `preset_not_found`), WS case | PASS |
| AC-20 | Shut down, cleanup | `test_ac16_ac20…`, `test_sigterm_graceful`, GUI `test_exit_and_stop_backend` | PASS |

Additional: duplicate backend refused (exit 3), secrets absent from logs (`test_log_contains_no_secrets`), CSRF/origin/rate limits, HttpOnly cookie not readable by JS, mobile layout without horizontal scroll (375 px), throttle coalescing, permanent vs transient errors, reconnect exhaustion, payload rejection keeps the connection, auto-switch matching/stop/manual override.

Note on SIGTERM: uvicorn ≥ 0.29 re-raises the captured signal after a graceful shutdown, so the exit code is -15 instead of 0. The test verifies that cleanup happened (activity cleared, `instance.json` removed, "backend stopped" logged).

## Not executed here (needs a real desktop)

| Item | Status | Why |
|---|---|---|
| Real Discord client | NOT RUN | no Discord in the CI container |
| OS system tray (icon, menu clicks, balloon) | NOT RUN | offscreen Qt has no tray |
| Windows named-pipe RPC, Proactor loop, firewall prompt | NOT RUN | Linux environment |
| Windows PyInstaller build | NOT RUN | Linux environment |

## Manual verification checklist (Windows + Discord desktop)

1. `scripts\windows\install.bat`, then `scripts\windows\run.bat`. Expect: a tray icon appears and the window opens.
2. Wizard: create an app in the Developer Portal, paste the ID, **Test connection** → "✓ Connected to Discord".
3. Finish with "Turn the presence on". Your Discord profile (check from another account or the user popout) shows *Playing \<app name\>* with details/state/timer.
4. Toggle OFF → the activity disappears within a few seconds. Toggle ON again.
5. Edit details → **Save & apply** → profile updates (≤ 4 s throttle).
6. Close the window → it hides; tray balloon shows once; presence stays. Click the tray icon → the window returns.
7. Tray → Open web dashboard → the browser is signed in. Toggle there → the GUI follows within a second.
8. Quit Discord → status "Reconnecting (attempt n …)". Start Discord → "Live on Discord" and the activity returns.
9. Enter a wrong Application ID in the wizard test → "Discord rejected the Application ID".
10. Settings → LAN access → restart backend → from a phone on the same Wi-Fi open `http://<PC-IP>:8765` → access-code login works; a wrong code is rejected; after 5 tries → "Too many login attempts".
11. Tray → Exit and stop backend → presence disappears; `python -m discord_pro status` → "Backend is not running."
12. `scripts\windows\build.bat` → run `dist\DiscordPro\DiscordPro.exe` and repeat steps 3, 6 and 11.

Record failures with `logs\backend.log` from `%APPDATA%\DiscordPro` and the output of Settings → Diagnostics.
