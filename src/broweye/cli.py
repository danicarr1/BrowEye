"""Command line entry point.

    broweye                      start UI + headless browser, project in ./broweye-project
    broweye --headed             show the browser window (needs a display)
    broweye lab --port 8081      start the local lab server used by the tests
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import webbrowser

from aiohttp import web


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="broweye", description="Observe, edit, replay and compare HTTP traffic.")
    sub = p.add_subparsers(dest="cmd")
    p.add_argument("--project", default="broweye-project", help="project directory (SQLite + saved sessions)")
    p.add_argument("--port", type=int, default=8765, help="UI port on 127.0.0.1 (0 = random)")
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("--no-browser", action="store_true", help="repeater/builder only, no browser capture")
    p.add_argument("--session", default="default", help="name of the first browser session")
    p.add_argument("--start-url", help="open this URL in the first session")
    p.add_argument("--open-ui", action="store_true", help="open the UI in your default browser")
    lab = sub.add_parser("lab", help="run the local lab server")
    lab.add_argument("--port", type=int, default=8081)
    return p


async def _run(args) -> None:
    from .app import BrowEye
    from .server import serve

    app = BrowEye(args.project, headless=not args.headed, with_browser=not args.no_browser)
    await app.start()
    runner, url = await serve(app, port=args.port)
    try:
        if app.browser:
            s = await app.open_session(args.session)
            if args.start_url:
                await app.navigate(s["id"], args.start_url)
        print(f"BrowEye UI: {url}", flush=True)
        print("(the token in the URL protects the local API; do not share it)", flush=True)
        if args.open_ui:
            webbrowser.open(url)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
                pass
        await stop.wait()
    finally:
        await runner.cleanup()
        await app.stop()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.cmd == "lab":
        from .lab import Lab, make_app
        print(f"Lab server on http://127.0.0.1:{args.port}/", flush=True)
        web.run_app(make_app(Lab()), host="127.0.0.1", port=args.port, print=None)
        return 0
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
