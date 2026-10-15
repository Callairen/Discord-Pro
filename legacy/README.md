# Legacy agents (v1)

The original repository contained three standalone scripts (`agents/*.py`) and their PyInstaller specs. Each script polled the process list every 5 s and, while a target program was running (`Code.exe`, `Figma.exe`, `VirtualBox*.exe`), connected to Discord with its own Application ID and published a fixed activity.

They are kept here unchanged for reference. Their behaviour now lives in the integrated app:

- each agent is a **preset** with its Application ID and process names, see `config/presets.example.json`;
- **Settings → Auto-switch** reproduces the process-watching behaviour through the single backend connection.

The prebuilt `build/` and `dist/` binaries were removed from version control. Audit notes on them: `docs/AUDIT.md`.
