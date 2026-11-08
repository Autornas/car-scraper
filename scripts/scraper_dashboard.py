#!/usr/bin/env python3
"""
scraper_dashboard.py — start the website (web/app.py).

    python scripts/scraper_dashboard.py               # http://127.0.0.1:8765/
    python scripts/scraper_dashboard.py --no-watcher  # don't start the watcher
    python scripts/scraper_dashboard.py --no-browser

With [web] auth = "local" (the default) it's yours alone, on this PC. To let
others in, put it behind Cloudflare Tunnel + Access and set auth =
"cloudflare" with the admin and viewer emails (see README, "Website").
"""

import argparse
import ipaddress
import sys
import webbrowser

import uvicorn

from settings import SETTINGS
from web.app import create_app


def is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def main():
    web = SETTINGS["web"]
    ap = argparse.ArgumentParser(description="Run the website and control dashboard.")
    ap.add_argument("--host", default=web["host"])
    ap.add_argument("--port", type=int, default=web["port"])
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-watcher", action="store_true",
                    default=not SETTINGS["dashboard"]["start_watcher"],
                    help="don't start the five-minute watcher on launch")
    args = ap.parse_args()
    if not 1 <= args.port <= 65535:
        ap.error("--port must be between 1 and 65535")
    # Local mode trusts whoever can reach it; that must only ever be this PC.
    # (Cloudflare Tunnel and Tailscale Serve also connect locally.)
    if not is_loopback(args.host):
        sys.exit(f"Refusing to listen on {args.host}: the site only listens on this PC "
                 "(127.0.0.1). Reach it from elsewhere through Tailscale Serve or "
                 "Cloudflare Tunnel + Access.")
    try:
        app = create_app(start_watcher=not args.no_watcher)
    except ValueError as exc:
        sys.exit(f"config.toml: {exc}")

    url = f"http://{args.host}:{args.port}/admin"
    print(f"Dashboard: {url}   (sign-in mode: {web['auth']})", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
