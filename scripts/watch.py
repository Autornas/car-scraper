#!/usr/bin/env python3
"""
watch.py — catch every new ad within minutes (see collectors/watch.py).

    python scripts/watch.py                # every enabled site, every 5 minutes
    python scripts/watch.py --once         # one scan, then exit
    python scripts/watch.py --verify 6     # are the last 6 h of ads all stored?
    python scripts/watch.py --site autoplius
"""

import argparse
import sys
import time

import requests

from collectors import enabled_sites, get_site
from collectors import watch as w
from collectors.store import Store
from settings import SETTINGS, project_path

CFG = SETTINGS["watcher"]


def main():
    ap = argparse.ArgumentParser(description="Catch every new ad and store it.")
    ap.add_argument("--site", action="append", default=None,
                    help="only this site (repeatable); default: every enabled site")
    ap.add_argument("--db", default=str(project_path(SETTINGS["paths"]["database"])))
    ap.add_argument("--interval-minutes", type=float, default=CFG["interval_minutes"])
    ap.add_argument("--margin-pages", type=int, default=CFG["margin_pages"])
    ap.add_argument("--max-pages", type=int, default=CFG["max_pages"])
    ap.add_argument("--delay", type=float, default=CFG["delay"])
    ap.add_argument("--no-read", dest="read_cheap_ads", action="store_false",
                    default=CFG["read_cheap_ads"], help="don't open the ads of cheap new cars")
    ap.add_argument("--once", action="store_true", help="run one scan and exit")
    ap.add_argument("--verify", type=float, metavar="HOURS",
                    help="check that every ad from the last HOURS is in the database")
    args = ap.parse_args()
    if args.interval_minutes <= 0 or args.max_pages < 1 or args.margin_pages < 0:
        ap.error("interval must be > 0, max pages >= 1, margin pages >= 0")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    sites = [get_site(s) for s in args.site] if args.site else enabled_sites()
    store = Store(args.db)
    watches = {site.SOURCE: w.WatchStore(store, site.SOURCE) for site in sites}
    session = requests.Session()
    try:
        if args.verify:
            for site in sites:
                w.verify(site, session, watches[site.SOURCE], args.verify, args.delay)
            return
        model, compiled = w.load_scoring()
        model_mtime = w.model_mtime()
        print(f"Watching {', '.join(s.SOURCE for s in sites)} every "
              f"{args.interval_minutes:g} min (stop after {args.margin_pages + 1} fully "
              f"known page(s), cap {args.max_pages} pages).")
        print("New ads are priced " + ("with the saved model." if model else
              "- no saved model yet: run autoplius_stats.py once to enable deal checks."))
        while True:
            started = time.monotonic()
            if w.model_mtime() != model_mtime:     # report rebuilt: use the new model
                model, compiled = w.load_scoring()
                model_mtime = w.model_mtime()
            for site in sites:
                try:
                    w.scan(site, session, store, watches[site.SOURCE], model, compiled, args)
                except requests.RequestException as exc:
                    print(f"  ! {site.SOURCE} scan failed: {exc}", file=sys.stderr)
            if args.once:
                break
            wait = args.interval_minutes * 60 - (time.monotonic() - started)
            time.sleep(max(5.0, wait))
    except KeyboardInterrupt:
        print("\nWatcher stopped.")
    finally:
        session.close()
        store.close()


if __name__ == "__main__":
    main()
