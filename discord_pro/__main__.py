"""Discord-Pro command line.

    python -m discord_pro                 # desktop GUI (starts the backend if needed)
    python -m discord_pro gui
    python -m discord_pro backend         # run the backend in the foreground
    python -m discord_pro status
    python -m discord_pro start           # start the backend in the background
    python -m discord_pro stop            # stop the background backend
    python -m discord_pro open-dashboard  # open the browser dashboard, signed in
    python -m discord_pro on | off        # toggle presence
"""

from __future__ import annotations

import argparse
import sys
import webbrowser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Frozen executables re-launch themselves with --backend.
    if argv[:1] == ["--backend"]:
        from discord_pro.backend.__main__ import main as backend_main

        return backend_main(argv[1:])

    parser = argparse.ArgumentParser(prog="discord-pro")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("gui")
    sub.add_parser("backend").add_argument("rest", nargs=argparse.REMAINDER)
    for name in ("status", "start", "stop", "open-dashboard", "on", "off"):
        sub.add_parser(name)
    args = parser.parse_args(argv)

    if args.cmd in (None, "gui"):
        from discord_pro.gui.main import main as gui_main

        return gui_main()
    if args.cmd == "backend":
        from discord_pro.backend.__main__ import main as backend_main

        return backend_main(args.rest)

    from discord_pro import local_client as lc

    if args.cmd == "start":
        info = lc.ensure_backend()
        print(f"Backend running at {info.url} (pid {info.pid})")
        return 0

    info = lc.discover()
    if info is None:
        print("Backend is not running.")
        return 0 if args.cmd in ("status", "stop") else 1

    if args.cmd == "status":
        status, state = lc.call(info, "GET", "/api/v1/state")
        if status != 200:
            print(f"Backend running at {info.url} but returned HTTP {status}")
            return 1
        p, d = state["presence"], state["discord"]
        print(f"Backend running at {info.url} (pid {info.pid}, v{state['application']['version']})")
        print(f"Presence: {'ON' if p['enabled'] else 'OFF'} - {p['connection_state']}")
        if d["last_error"]:
            print(f"Last error: {d['last_error']['message']}")
        return 0
    if args.cmd == "stop":
        status, _ = lc.call(info, "POST", "/api/v1/shutdown")
        print("Stopping backend..." if status == 200 else f"Shutdown failed (HTTP {status})")
        return 0 if status == 200 else 1
    if args.cmd == "open-dashboard":
        url = lc.dashboard_login_url(info)
        webbrowser.open(url)
        print(f"Opened {info.url}")
        return 0
    if args.cmd in ("on", "off"):
        status, body = lc.call(info, "POST", f"/api/v1/presence/{'enable' if args.cmd == 'on' else 'disable'}")
        result = (body or {}).get("result", {})
        if result.get("success"):
            print(result.get("message") or "OK")
            return 0
        err = result.get("error") or (body or {}).get("error") or {}
        print(f"Failed: {err.get('message', status)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
