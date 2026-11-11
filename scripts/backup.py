#!/usr/bin/env python3
"""
backup.py — copy the database to another drive every few days.

Everything this project knows lives in one file (data/autoplius.db): listings,
price history, labels, the watchlist, the audit log. This copies it, safely
while the scrapers are writing, to [backup] dir in config.toml (set it in
config.local.toml: the folder belongs to one PC), keeps the newest `keep`
copies and deletes older ones. config.local.toml is copied next to them.

    python scripts/backup.py            # back up now
    python scripts/backup.py --if-due   # only if the newest copy is older than every_days

The dashboard does the --if-due check by itself every half hour while it
runs, so no scheduled task is needed; its Database card shows the last backup
and any failure (drive missing, disk full).

To restore: stop the dashboard, copy a backup over data/autoplius.db (delete
autoplius.db-wal and autoplius.db-shm if they are there), start it again.
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from settings import PROJECT_ROOT, SETTINGS, project_path

CFG = SETTINGS["backup"]
PREFIX = "car-scraper-"
LAST_ERROR = [None]          # the last failure, for the dashboard; None once one works


def backup_dir(cfg=CFG):
    """The folder backups go to, or None when backups are switched off."""
    return Path(cfg["dir"]) if cfg["dir"].strip() else None


def backups(dest):
    """Finished backups in `dest`, oldest first (the names sort by date)."""
    try:
        return sorted(Path(dest).glob(f"{PREFIX}*.db"))
    except OSError:
        return []


def last_backup(dest):
    """When the newest backup was made (UTC), or None."""
    found = backups(dest)
    if not found:
        return None
    return datetime.fromtimestamp(found[-1].stat().st_mtime, timezone.utc)


def is_due(dest, every_days, now=None):
    last = last_backup(dest)
    if last is None:
        return True
    now = now or datetime.now(timezone.utc)
    # an hour's slack, so "every 3 days" doesn't drift to every 3 days and a bit
    return (now - last).total_seconds() >= every_days * 86400 - 3600


def run(db_path, dest, keep=5, extras=()):
    """Copy the database into `dest` and prune old copies. Returns (path of
    the new backup, listings in it). Raises OSError / sqlite3.Error /
    RuntimeError if it could not make a good copy; a partial file is removed."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / f"{PREFIX}{datetime.now():%Y-%m-%d_%H%M%S}.db"
    part = final.with_suffix(".db.part")
    # SQLite's own backup: a consistent copy even while the watcher is writing.
    source = sqlite3.connect(db_path, timeout=60)
    try:
        target = sqlite3.connect(part)
        try:
            source.backup(target)
            # A plain single file on the backup drive: no -wal / -shm beside it.
            target.execute("PRAGMA journal_mode=DELETE")
            check = target.execute("PRAGMA integrity_check").fetchone()[0]
            listings = target.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        finally:
            target.close()
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        source.close()
    if check != "ok":
        part.unlink(missing_ok=True)
        raise RuntimeError(f"the copy failed its integrity check: {check}")
    os.replace(part, final)
    for extra in extras:                 # small per-PC files worth having with it
        if Path(extra).is_file():
            shutil.copy2(extra, dest / Path(extra).name)
    for old in backups(dest)[:-max(1, keep)]:
        try:
            old.unlink()
        except OSError:
            pass
    return final, listings


def run_configured(cfg=CFG):
    """One backup with the settings from config.toml. Returns a message."""
    dest = backup_dir(cfg)
    if dest is None:
        return "Backups are off: set [backup] dir in config.local.toml."
    path, listings = run(project_path(SETTINGS["paths"]["database"]), dest, cfg["keep"],
                         extras=[PROJECT_ROOT / "config.local.toml"])
    size = path.stat().st_size / 1e6
    return (f"Backup written: {path} ({size:.1f} MB, {listings:,} listings); "
            f"{len(backups(dest))} kept.")


def run_if_due(cfg=CFG):
    """What the dashboard calls every half hour. Never raises: the failure is
    kept for the dashboard to show."""
    dest = backup_dir(cfg)
    if dest is None:
        return None
    try:
        if not is_due(dest, cfg["every_days"]):
            return None
        message = run_configured(cfg)
        LAST_ERROR[0] = None
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        LAST_ERROR[0] = f"{exc.__class__.__name__}: {exc}"[:300]
        message = f"Backup failed: {LAST_ERROR[0]}"
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] {message}", flush=True)
    return message


def run_forever(stop, cfg=CFG, check_seconds=1800):
    """Check now, then every `check_seconds`, until `stop` (an Event) is set."""
    while True:
        run_if_due(cfg)
        if stop.wait(check_seconds):
            return


def status(cfg=CFG):
    """For the dashboard: where backups go, when the last one was, what failed."""
    dest = backup_dir(cfg)
    if dest is None:
        return None
    last = last_backup(dest)
    return {"dir": str(dest), "every_days": cfg["every_days"], "count": len(backups(dest)),
            "last": last.isoformat(timespec="seconds") if last else None,
            "error": LAST_ERROR[0]}


def main():
    ap = argparse.ArgumentParser(description="Back up the database to [backup] dir.")
    ap.add_argument("--if-due", action="store_true",
                    help="only if the newest backup is older than [backup] every_days")
    args = ap.parse_args()
    dest = backup_dir()
    if dest is None:
        sys.exit("Backups are off: set [backup] dir in config.local.toml, "
                 'e.g. dir = "D:/car-scraper-backups".')
    if args.if_due and not is_due(dest, CFG["every_days"]):
        print(f"Not due yet: the newest backup is from {last_backup(dest):%Y-%m-%d %H:%M} UTC.")
        return 0
    try:
        print(run_configured())
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"Backup failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
