"""Start the Agentic OS backend.

    python -m agentic_os                 # browser mode: prints a pairing code, serves the built UI
    python -m agentic_os --token-stdin   # desktop mode: the Tauri shell writes a bearer token on stdin

Prints one line `AGENTIC_OS_READY {...}` on stdout once the server is listening.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import webbrowser


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agentic_os", description="Agentic OS local backend")
    parser.add_argument("--port", type=int, default=None, help="port (0 = pick a free one)")
    parser.add_argument("--data-dir", default=None, help="override the data directory")
    parser.add_argument("--token-stdin", action="store_true", help="read a bearer token from the first stdin line")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    parser.add_argument("--dev", action="store_true", help="allow the Vite dev server origin")
    args = parser.parse_args(argv)

    if args.data_dir:
        os.environ["AGENTIC_OS_DATA_DIR"] = args.data_dir
    from .config import Settings

    overrides = {}
    if args.port is not None:
        overrides["port"] = args.port or _free_port()
    else:
        wanted = int(os.environ.get("AGENTIC_OS_PORT", "8765"))
        if not _port_free(wanted):
            overrides["port"] = _free_port()
            print(f"Port {wanted} is busy; using {overrides['port']} instead.", file=sys.stderr)
    if args.dev:
        overrides["dev_mode"] = True
    if args.token_stdin:
        token = sys.stdin.readline().strip()
        if len(token) < 32:
            print("refusing to start: --token-stdin needs a token of at least 32 characters", file=sys.stderr)
            return 2
        overrides["bearer_token"] = token
        overrides["open_browser"] = False
    if args.no_browser:
        overrides["open_browser"] = False
    settings = Settings.from_env(**overrides)

    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    import uvicorn

    from .api.app import create_app
    from .api.state import AppState

    state = AppState(settings)
    app = create_app(settings, state)
    url = f"http://127.0.0.1:{settings.port}"
    if not settings.bearer_token:
        def show(code: str) -> None:
            print(f"\n  Agentic OS is running at {url}\n  Pairing code: {code}  (valid {settings.pairing_code_ttl_s // 60} min)\n",
                  file=sys.stderr, flush=True)

        state.auth.set_rotation_callback(show)
        code = state.auth.current_code()
        show(code)
        if settings.open_browser:
            # The one-time code travels in the URL fragment, which browsers never send to a server,
            # so the window this launcher opens pairs itself. It expires after one use or 10 minutes.
            threading.Timer(1.5, lambda: webbrowser.open(f"{url}/#pair={code}")).start()
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning", access_log=False,
                            lifespan="on")
    server = uvicorn.Server(config)
    if args.token_stdin:
        # The desktop shell keeps our stdin open for its whole life. EOF means the shell exited or
        # crashed, so shut down instead of lingering as an orphan holding the port and the database.
        def watch_parent() -> None:
            try:
                while sys.stdin.read(4096):
                    pass
            except Exception:  # noqa: BLE001
                pass
            # Shut down first: stderr is a pipe to the same (dead) parent, so writing to it may fail.
            server.should_exit = True
            threading.Timer(10.0, lambda: os._exit(0)).start()
            try:
                print("desktop shell went away; shutting down", file=sys.stderr, flush=True)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=watch_parent, name="aos-parent-watch", daemon=True).start()
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
