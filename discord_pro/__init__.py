"""Discord-Pro: integrated Discord Rich Presence manager.

Components:
- ``discord_pro.backend``: local FastAPI service that owns the Discord RPC
  connection and the canonical application state.
- ``discord_pro.gui``: PySide6 desktop client with system tray integration.
- ``discord_pro/web``: browser dashboard served by the backend.
"""

__version__ = "2.0.0"
