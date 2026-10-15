"""Run the backend: ``python -m discord_pro.backend``."""

from __future__ import annotations

import argparse
import logging
import sys

from discord_pro import __version__, paths
from discord_pro.backend.instance import (
    AlreadyRunning,
    InstanceLock,
    remove_instance_file,
    write_instance_file,
)
from discord_pro.backend.logging_setup import setup_logging

log = logging.getLogger("discord_pro.backend")

EXIT_ALREADY_RUNNING = 3
EXIT_STARTUP_FAILED = 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="discord-pro-backend", description="Discord-Pro local backend")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--port", type=int, help="override the configured port for this run")
    args = parser.parse_args(argv)

    setup_logging(paths.log_dir(), args.log_level)
    lock = InstanceLock(paths.lock_path())
    try:
        lock.acquire()
    except AlreadyRunning:
        log.error("another backend instance is already running (data dir: %s)", paths.app_data_dir())
        return EXIT_ALREADY_RUNNING

    try:
        import uvicorn

        from discord_pro.backend.app import create_app

        server_holder: dict[str, uvicorn.Server] = {}

        def request_shutdown() -> None:
            server = server_holder.get("server")
            if server is not None:
                server.should_exit = True

        app = create_app(request_shutdown=request_shutdown)
        cfg = app.state.backend.config.config
        port = args.port or cfg.server.port
        host = cfg.server.bind_host()
        if args.port:
            app.state.backend.state._state["server"]["port"] = port  # reflect override in state
        if cfg.server.allow_lan:
            log.warning("LAN access is ENABLED: other devices on your network can reach the dashboard "
                        "(access code required)")

        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_config=None,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            ws_max_size=64 * 1024,
            ws_ping_interval=20,
            ws_ping_timeout=20,
            timeout_graceful_shutdown=8,
        )
        server = uvicorn.Server(config)
        server_holder["server"] = server

        original_startup = server.startup

        async def startup(sockets=None):
            await original_startup(sockets=sockets)
            if server.started:
                write_instance_file(paths.instance_path(), port=port, bind_host=host, version=__version__)
                log.info("dashboard: http://127.0.0.1:%d  (open it from the desktop app or with "
                         "'python -m discord_pro open-dashboard')", port)

        original_shutdown = server.shutdown

        async def shutdown(sockets=None):
            # Runs before uvicorn re-raises a captured SIGTERM/SIGINT, so the
            # discovery file is removed even when the process ends by signal.
            try:
                await original_shutdown(sockets=sockets)
            finally:
                remove_instance_file(paths.instance_path())

        server.startup = startup  # type: ignore[method-assign]
        server.shutdown = shutdown  # type: ignore[method-assign]
        server.run()
        if not server.started:
            log.error("server failed to start (is port %d in use?)", port)
            return EXIT_STARTUP_FAILED
        return 0
    finally:
        remove_instance_file(paths.instance_path())
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
