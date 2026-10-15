# Discord-Pro

Integrated **Discord Rich Presence manager** for the Discord desktop client:

- **Backend engine** – a local service that owns the single Discord RPC connection and the canonical state (REST + WebSocket API).
- **Desktop GUI** – PySide6 app with a big ON/OFF toggle, activity editor, presets, setup wizard and a **system tray** icon. Closing the window only hides it; presence keeps running.
- **Web dashboard** – the same controls in a browser (phone-friendly), served by the backend, synchronized in real time.

All three are one application: the GUI and the browser are both clients of the same backend, so a change made in one appears in the other immediately.

> Bahasa Indonesia: lihat [Mulai cepat](#mulai-cepat-bahasa-indonesia) di bawah.

## Features

| | |
|---|---|
| Presence ON/OFF | One click (GUI, tray menu, browser, or `python -m discord_pro on/off`). |
| Activity editor | Activity type, details, state, large/small image (asset key or https URL), tooltips, up to 2 buttons, elapsed timer. Validated before anything is sent. "Save" vs "Save & apply" are separate. |
| Presets | Create, edit, duplicate, delete (with confirmation), activate, activate & turn on. Each preset may use its own Discord Application ID. |
| Auto-switch | Optional: activate a preset while its program runs (e.g. `Code.exe`). This keeps what the original `agents/*.py` scripts did, now as a setting. |
| Setup wizard | Explains the Developer Portal steps, validates the Application ID, tests a real RPC handshake, saves, optional test activation. |
| Reliability | Explicit RPC states (`DISCONNECTED`, `CONNECTING`, `CONNECTED`, `RECONNECTING`, `ERROR`, `STOPPING`), bounded exponential backoff with jitter, Discord crash/restart detection, update throttling (Discord allows 5 updates / 20 s). |
| Security | Binds to `127.0.0.1` by default; LAN access is opt-in and authenticated; DNS-rebinding and CSRF protection; secrets never reach the browser or the logs. See [docs/SECURITY.md](docs/SECURITY.md). |

## What Discord requires (and what this app cannot do)

- The **Discord desktop app** must run on the same computer. Rich Presence uses Discord's local RPC pipe; the browser version of Discord cannot be used.
- You create a free **application** in the [Discord Developer Portal](https://discord.com/developers/applications) and paste its **Application ID**. Its name is what appears after "Playing". **Discord offers no API to create applications automatically**, so this step is manual (the wizard walks you through it). Automating it with an account token would violate Discord's Terms of Service, so it is deliberately not implemented.
- In Discord: *User Settings → Activity Privacy → Share my activity* must be on.
- Activity buttons are visible to other people, not on your own profile.
- This is *Rich Presence* (the activity card), not the custom status text/emoji, which apps cannot set.

## Install

Requires Python 3.10+ (tested with 3.13).

```bash
git clone https://github.com/Callairen/Discord-Pro.git
cd Discord-Pro
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

On Windows you can instead double-click `scripts\windows\install.bat`, then `scripts\windows\run.bat`.

## Run

```bash
python -m discord_pro            # desktop GUI (starts the backend in the background if needed)
```

First run: the dashboard shows **Finish setup → Open setup wizard**.

Other commands:

```bash
python -m discord_pro backend         # run the backend in the foreground (logs to the console)
python -m discord_pro start           # start the backend in the background
python -m discord_pro status          # backend + presence status
python -m discord_pro on | off        # toggle presence
python -m discord_pro open-dashboard  # open the web dashboard, already signed in
python -m discord_pro stop            # stop the backend (clears the presence)
```

### Web dashboard

- From the GUI: **Open web dashboard** (or the tray menu). This signs the browser in with a one-time link.
- Other devices on your Wi-Fi: enable **Settings → LAN access**, restart the backend, open `http://<this-PC-IP>:8765` and sign in with the **access code** shown in the desktop app's Settings. Your firewall may ask to allow Python/DiscordPro on private networks.

### Lifecycle

| Action | Effect |
|---|---|
| Close the window (✕) | Window hides to the tray; backend and presence keep running. |
| Tray → Show window / click the icon | Window returns with the current state. |
| Tray → Exit (keep presence running) | GUI quits; backend keeps running (reopen the GUI any time). |
| Tray → Exit and stop backend | Presence is cleared, backend stops, GUI quits. |
| Close the browser tab | No effect on the backend. |

The backend is a supervised background process, **not** an OS service: it does not start at login or survive logout/reboot unless you add `python -m discord_pro start` (or the built executable with `--backend`) to your startup programs. Only one backend runs per user (OS file lock); a second launch exits with code 3.

## Configuration

Settings live in a per-user data directory (override with `DISCORD_PRO_HOME`):

| OS | Directory |
|---|---|
| Windows | `%APPDATA%\DiscordPro` |
| macOS | `~/Library/Application Support/DiscordPro` |
| Linux | `~/.config/discord-pro` |

Files: `config.json` (settings, see [config/config.example.json](config/config.example.json)), `presets.json` (see [config/presets.example.json](config/presets.example.json), which contains the three original agents converted to presets), `auth.json` (API token + access code, user-only permissions), `logs/backend.log`.

Edit settings in the app rather than by hand; the backend validates every change. A malformed or partially invalid file is backed up and only the invalid sections are reset.

To import the original agents: copy `config/presets.example.json` to the data directory as `presets.json` (or recreate them in the Presets tab), then enable **Settings → Auto-switch**.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/unit tests/integration        # fast, any OS
python -m pytest tests/e2e tests/gui                 # real backend process, browser, Qt (Linux/macOS)
```

E2E/GUI tests use a Discord IPC simulator (Unix socket) and need Chromium for Playwright (`python -m playwright install chromium`) and `QT_QPA_PLATFORM=offscreen` on headless machines. Results and the manual Discord checklist: [docs/TESTING.md](docs/TESTING.md).

## Build a Windows executable

```bat
pip install -r requirements-dev.txt
pyinstaller packaging\DiscordPro.spec --noconfirm
```

Output: `dist\DiscordPro\DiscordPro.exe` (one-folder build). The same executable runs the GUI; it starts its own backend with `DiscordPro.exe --backend`. The spec was verified by building and running on Linux; a Windows build has **not** been produced or tested yet (see [docs/TESTING.md](docs/TESTING.md)).

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Discord desktop client not detected" | Start the Discord **desktop** app (not the browser). The backend keeps retrying with backoff. |
| "Discord rejected the Application ID" | Copy the *Application ID* (not the public key or a token) from the Developer Portal → General Information. |
| Presence shows "Live" but nothing on the profile | Turn on *Share my activity*; check you are not invisible; image keys must match uploaded Art Assets (uploads can take minutes to appear). |
| "Gave up after N attempts" | Start Discord, then toggle presence again (limit in Settings → Reconnect). |
| Phone cannot open the dashboard | Enable LAN access **and restart the backend**; use the PC's IP address (not a hostname); allow it through the firewall on private networks. |
| Logs | `logs/backend.log` in the data directory, or **Settings → Diagnostics**. |

## Project layout

```
discord_pro/
  backend/   app.py (REST+WS), service.py, manager.py (RPC state machine), rpc.py (pypresence adapter),
             activity.py (payload validation), config.py, presets.py, security.py, state.py, instance.py
  gui/       main.py, main_window.py (+tray), dialogs.py (wizard, preset editor), client.py, widgets.py
  web/       index.html, app.js, api.js, style.css (no build step)
  local_client.py, __main__.py (CLI)
tests/       unit/, integration/, e2e/, gui/, fakes.py (fake adapter + Discord IPC simulator)
docs/        ARCHITECTURE.md, SECURITY.md, TESTING.md, AUDIT.md, screenshots/
legacy/      the original agents and PyInstaller specs (kept for reference)
```

---

## Mulai cepat (Bahasa Indonesia)

1. Pasang Python 3.10+ lalu jalankan `pip install -r requirements.txt` (di Windows cukup klik dua kali `scripts\windows\install.bat`).
2. Jalankan `python -m discord_pro` (atau `scripts\windows\run.bat`). Aplikasi desktop terbuka dan backend otomatis berjalan di belakang.
3. Klik **Open setup wizard**, ikuti langkahnya: buat aplikasi di Discord Developer Portal, tempel **Application ID**, isi teks status, lalu **Test connection**.
4. Pakai tombol besar **Presence ON/OFF** untuk menyalakan/mematikan status. Tab **Presets** untuk menyimpan beberapa status dan berpindah dengan satu klik.
5. Menutup jendela hanya menyembunyikannya ke system tray; status tetap tampil. Untuk keluar total: ikon tray → **Exit and stop backend**.
6. Dashboard browser: tombol **Open web dashboard**. Untuk HP di Wi-Fi yang sama: aktifkan **Settings → LAN access**, restart backend, buka `http://IP-PC:8765`, masuk dengan **access code** dari Settings.

Catatan: Discord desktop harus terbuka, dan pembuatan aplikasi di Developer Portal memang harus manual (Discord tidak menyediakan API untuk itu).
