#!/usr/bin/env python3
"""
update.py — bring the whole database up to date in one run.

    1. scrape      every enabled site AT THE SAME TIME (they are different
                   servers, so neither waits for the other): each site's models
                   that are due, then its damage sweep
    2. rebuild     the price model and the report
    3. read ads    of the best "unchecked" candidates on every site, again at
                   the same time, so they get a real verdict
    4. rebuild     the report with those verdicts

Every line a site's scraper prints starts with "[autoplius] " or
"[autogidas] ", so it is always clear which site answered what. One site
being refused doesn't stop the other, and the report is rebuilt either way.

    python scripts/update.py             # everything that is due
    python scripts/update.py --force     # every model again, however fresh
    python scripts/update.py --nightly   # at most <site>.nightly_limit models per
                                         # site and report.nightly_reads ad reads
    python scripts/update.py --site autoplius

The dashboard's "Update database" button runs this; nightly.py runs it with
--nightly.
"""

import argparse
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

from collectors import enabled_sites, get_site
from collectors.store import CANONICAL_SOURCE
from settings import SETTINGS, project_path

SCRIPTS = Path(__file__).resolve().parent
CANDIDATES = project_path(SETTINGS["paths"]["data_dir"]) / "candidates.txt"
LOCK_PATH = project_path(SETTINGS["paths"]["data_dir"]) / "update.lock"

_print_lock = threading.Lock()
_children = []
_stopping = threading.Event()


def say(text):
    with _print_lock:
        print(text, flush=True)


def spawn(script, *args):
    process = subprocess.Popen(
        [sys.executable, "-u", str(SCRIPTS / script), *args],
        cwd=SCRIPTS.parent, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    _children.append(process)
    return process


def pump(process, tag=None):
    """Pass a child's output on, line by line; lines the child didn't tag with
    its site get the tag here. Returns the exit code."""
    prefix = f"[{tag}] " if tag else ""
    for line in process.stdout:
        line = line.rstrip("\n")
        if prefix and line.strip() and not line.lstrip().startswith(prefix):
            line = prefix + line
        say(line)
    return process.wait()


def run_one(script, *args):
    return pump(spawn(script, *args))


def run_per_site(sites, args_for):
    """One scrape.py per site, all at once. Returns {site name: exit code}."""
    codes = {}

    def work(site):
        codes[site.SOURCE] = pump(
            spawn("scrape.py", "--site", site.SOURCE, *args_for(site)), site.SOURCE)

    threads = [threading.Thread(target=work, args=(site,)) for site in sites]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return codes


def catalogue_missing(db_path):
    """True on a database that has no shared make/model catalogue yet. Other
    sites file their cars under it, so it has to exist before they start."""
    if not Path(db_path).exists():
        return True
    connection = sqlite3.connect(db_path, timeout=10)
    try:
        return connection.execute(
            "SELECT COUNT(*) FROM models WHERE source = ?", (CANONICAL_SOURCE,)).fetchone()[0] == 0
    except sqlite3.Error:
        return True
    finally:
        connection.close()


def only_one(lock_path):
    """Hold a lock for as long as this run lives, or return None if another
    update (the nightly one, or the dashboard's) already holds it. Two at once
    would double the requests each site gets and earn a rate limit."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def stop_children(*_):
    _stopping.set()
    for process in _children:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description="Scrape every site, check candidates, "
                                             "rebuild the report.")
    ap.add_argument("--site", action="append", default=None,
                    help="only this site (repeatable); default: every enabled site")
    ap.add_argument("--force", action="store_true", help="re-scrape every model")
    ap.add_argument("--nightly", action="store_true",
                    help="the nightly amounts: nightly_limit models, nightly_reads ad reads")
    ap.add_argument("--reads", type=int, default=None,
                    help="ad pages to read per site (default: report.candidates, "
                         "or report.nightly_reads with --nightly; 0 = none)")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), stop_children)
            except (ValueError, OSError):
                pass

    sites = [get_site(s) for s in args.site] if args.site else enabled_sites()
    if not sites:
        sys.exit("No site is enabled in config.toml.")
    lock = only_one(LOCK_PATH)
    if lock is None:
        say("Another update is already running (the nightly one, or one started from the "
            "dashboard). Not starting a second.")
        return 0
    names = " and ".join(site.SOURCE for site in sites)
    report = SETTINGS["report"]
    reads = args.reads if args.reads is not None else (
        report["nightly_reads"] if args.nightly else report["candidates"])
    total = 4 if reads else 2
    failed = []

    def scrape_args(site):
        extra = ["--force"] if args.force else []
        if args.nightly:
            extra += ["--limit", str(SETTINGS[site.SOURCE]["nightly_limit"])]
        return extra

    db_path = project_path(SETTINGS["paths"]["database"])
    if len(sites) > 1 and catalogue_missing(db_path):
        say("▶ first run: fetching the shared make/model catalogue")
        run_one("scrape.py", "--site", CANONICAL_SOURCE, "--limit", "0")

    say(f"▶ step 1 of {total}: scraping {names}"
        + (" at the same time" if len(sites) > 1 else "")
        + (" (every model)" if args.force else " (models that are due)")
        + ", then flagging damaged cars")
    for site, code in run_per_site(sites, scrape_args).items():
        if code != 0:
            failed.append(f"scrape {site}")

    if not _stopping.is_set():
        say(f"\n▶ step 2 of {total}: rebuilding the price model and the report")
        if run_one("autoplius_stats.py") != 0:
            failed.append("report")
            reads = 0                      # no candidate list to read from

    if reads and not _stopping.is_set() and CANDIDATES.exists():
        say(f"\n▶ step 3 of {total}: reading the ads of the best unchecked cars "
            f"(up to {reads} per site)")
        codes = run_per_site(sites, lambda site: [
            "--backfill-descriptions", "--ids-file", str(CANDIDATES),
            "--description-limit", str(reads)])
        failed += [f"ad reads {site}" for site, code in codes.items() if code != 0]
        if not _stopping.is_set():
            say(f"\n▶ step 4 of {total}: rebuilding the report with their verdicts")
            if run_one("autoplius_stats.py") != 0:
                failed.append("report")

    if _stopping.is_set():
        say("\nStopped. Everything scraped so far is saved; the next run resumes.")
        return 1
    say("\nUpdate " + ("finished." if not failed else "had failures: " + ", ".join(failed) + "."))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
