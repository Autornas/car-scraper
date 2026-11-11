#!/usr/bin/env python3
"""
nightly.py — keep the database fresh without anyone pressing a button.

Runs update.py with the nightly amounts (see update.py for the steps):

    1. every enabled site at the same time: sync its catalogue (weekly),
       re-scrape up to <site>.nightly_limit models older than max_age_days
       (oldest first; models with no ads only every empty_max_age_days), then
       its site-wide damage sweep
    2. rebuild the report and the saved price model, which a running watcher
       picks up by itself
    3. read the ads of up to report.nightly_reads of the best "unchecked" cars
       per site, and rebuild the report so they get a real verdict

Per site, at most nightly_limit models a night, oldest first: a full refresh
spreads over the week instead of one long burst the site rate-limits.

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


def main():
    log_dir = project_path(SETTINGS["paths"]["data_dir"]) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"nightly-{datetime.now():%Y-%m-%d}.log"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n=== {datetime.now():%H:%M:%S} nightly update ===\n")
        log.flush()
        code = subprocess.call([sys.executable, "-u", str(SCRIPTS / "update.py"), "--nightly"],
                               cwd=SCRIPTS.parent, stdout=log, stderr=subprocess.STDOUT,
                               env=env)
        log.write(f"=== {datetime.now():%H:%M:%S} exit code {code} ===\n")
    print(f"Nightly refresh {'finished' if code == 0 else 'had failures'}. Log: {log_path}")
    return 1 if code else 0


if __name__ == "__main__":
    sys.exit(main())
