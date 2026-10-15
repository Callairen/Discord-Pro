# Phase 0 – Repository audit (baseline: commit 6ac230a, 2026-06-24)

## 1. Existing technology stack

- Python 3 scripts using `pypresence` (Discord local RPC) and `psutil` (process list).
- PyInstaller (`*.spec`, windowed, UPX), built on Windows (`C:\Users\LOQ\Discord Pro\…`).
- No dependency manifest, README, tests, CI, configuration files, server, GUI or web UI.

## 2. Existing functional features

`agents/vscode_agent.py`, `agents/kali_agent.py`, `agents/figma.py`: every 5 s, check whether a target process runs (`Code.exe`; `VirtualBox.exe`/`VirtualBoxVM.exe`; `Figma.exe`). On start: `Presence(CLIENT_ID).connect()` + `update(...)` with a hard-coded activity; on exit: `clear()` + `close()`. Each agent has its own Application ID.

## 3. Incomplete or broken behaviour

- No error handling: if Discord is not running, `rpc.connect()` raises and the agent crashes. Discord quitting while active also crashes it on the next call.
- `figma.py` prints "Kali Linux agent started…" (copy-paste).
- `kali_agent.spec` points to `kali_agent.py` at the repo root, which does not exist there; it only built from an earlier layout.
- Three agents means up to three simultaneous RPC connections, with no coordination between them.
- No way to change texts, images or buttons without editing code and rebuilding.
- No ON/OFF control other than starting or stopping the program.

## 4. Security and reliability concerns

- `build/` and `dist/` (three ~9.6 MB `.exe` files plus intermediate archives) were committed. Binaries in source control cannot be reviewed, bloat the repository, and are a common malware vector.
  - **Verification performed:** each `dist/*.exe` was opened as a PyInstaller archive (without executing it) and the entry script's code object inspected. Each contains only `pypresence`, `psutil` and stdlib modules. Its names and constants match the corresponding source file (Application IDs, process names, messages). No network, credential or persistence code was found. SHA-256:
    - `figma.exe` f9d4feefe5d5c41c78e897429f44f8fd0d277bbdbe1ea807473a16735f1e0f82
    - `kali_agent.exe` 76cfe6c51361079f5663e634cd8736a0ff79f0a248ed72cd033c2ee8de02d855
    - `vscode_agent.exe` 2db7ae0fdc8b8f3b9623318c6d4159beda2a6ba8509c382030c0c4e5388196c2
  - Decision: remove them from version control (`.gitignore` now blocks build output) and keep sources under `legacy/`.
- Busy loop without backoff; `print` instead of logging; windowed builds discard all output.

## 5. Reusable components

- The pypresence + psutil approach and the three activity definitions (converted to presets in `config/presets.example.json`).
- The process-detection logic (now `service.running_process_names` + `auto_switch_tick`).
- PyInstaller as the packaging tool (new spec in `packaging/`).

## 6. Required changes

Backend with a single RPC owner and state machine, REST + WebSocket API, persistence, auth/network policy, desktop GUI with tray, web dashboard, tests, docs. Nothing existing could be extended in place, so the implementation is new code; the legacy scripts are preserved unchanged.

## 7. Implementation sequence (as executed)

1. Config/validation/presets → 2. RPC adapter + state machine → 3. service, state bus, REST, WS, security → 4. tests with fake adapter + IPC simulator → 5. web dashboard → 6. CLI + launcher → 7. GUI + tray → 8. E2E/GUI tests → 9. packaging → 10. docs.

## 8. Risks / constraints verified

- Discord docs (Activity object): max 2 buttons, label 1–32, URL 1–512; 5 activity updates per 20 s. Text-length limits are not published; 2–128 is enforced from observed RPC behaviour and kept in one place (`activity.py`).
- pypresence 4.6.2 API checked (`AioPresence.update` supports `activity_type`, `buttons`; `close()` closes the event loop, which led to a custom close).
- Discord offers no API to create Developer Portal applications; the wizard guides the manual steps.
- No real Discord client was available in the development environment (see TESTING.md).
