#!/usr/bin/env python3
"""
nightly.py — keep the database fresh without anyone pressing a button.

    1. autoplius_db.py     syncs the make/model catalogue (weekly), re-scrapes
                           every model older than autoplius.max_age_days (models
                           with no ads only every empty_max_age_days), then runs
                           the site-wide damage sweep
    2. autoplius_stats.py  rebuilds the report and the saved price model, which
                           a running watcher picks up by itself

Output goes to data/logs/nightly-YYYY-MM-DD.log. Run it by hand, or let Windows
run it every night: scripts/install_nightly_task.ps1 registers that.

    python scripts/nightly.py
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from settings import SETTINGS, project_path

SCRIPTS = Path(__file__).resolve().parent
STEPS = [
    ("refresh listings", ["autoplius_db.py"]),
    ("rebuild report and price model", ["autoplius_stats.py"]),
]


def main():
    log_dir = project_path(SETTINGS["paths"]["data_dir"]) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"nightly-{datetime.now():%Y-%m-%d}.log"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    failed = []
    with log_path.open("a", encoding="utf-8") as log:
        for name, args in STEPS:
            log.write(f"\n=== {datetime.now():%H:%M:%S} {name} ===\n")
            log.flush()
            code = subprocess.call([sys.executable, "-u", str(SCRIPTS / args[0]), *args[1:]],
                                   cwd=SCRIPTS.parent, stdout=log, stderr=subprocess.STDOUT,
                                   env=env)
            log.write(f"=== {datetime.now():%H:%M:%S} {name}: exit code {code} ===\n")
            if code != 0:
                failed.append(name)
    print(f"Nightly refresh {'finished' if not failed else 'had failures: ' + ', '.join(failed)}."
          f" Log: {log_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
