#!/usr/bin/env python3
"""Periodically save listings newly appearing in the latest search pages."""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

import autoplius_scraper as scraper

from settings import SETTINGS, project_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = project_path(SETTINGS["paths"]["data_dir"])
CFG = SETTINGS["watcher"]


class SeenStore:
    """Remember observed ad ids across watcher restarts."""

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS observed_ads (
                id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS watcher_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scan_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                status TEXT NOT NULL,
                pages_checked INTEGER NOT NULL,
                listings_seen INTEGER NOT NULL,
                new_count INTEGER NOT NULL,
                details_json TEXT NOT NULL,
                message TEXT NOT NULL
            );
        """)
        self.conn.commit()

    def is_primed(self):
        row = self.conn.execute(
            "SELECT value FROM watcher_state WHERE key = 'primed'"
        ).fetchone()
        return row is not None and row[0] == "1"

    def observed_ids(self):
        return {row[0] for row in self.conn.execute("SELECT id FROM observed_ads")}

    def record_scan(self, ad_ids, prime=False):
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO observed_ads (id) VALUES (?)",
                ((ad_id,) for ad_id in ad_ids),
            )
            if prime:
                self.conn.execute(
                    """INSERT INTO watcher_state (key, value) VALUES ('primed', '1')
                       ON CONFLICT(key) DO UPDATE SET value = '1'"""
                )

    def record_run(self, started_at, status, pages, rows, new_rows, message=""):
        details = {
            "pages": pages,
            "new_listings": [
                {key: row.get(key, "") for key in ("id", "title", "year", "price_eur", "link")}
                for row in new_rows
            ],
        }
        with self.conn:
            self.conn.execute(
                """INSERT INTO scan_history
                   (started_at, finished_at, status, pages_checked, listings_seen,
                    new_count, details_json, message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    started_at,
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                    status,
                    sum(1 for page in pages if page["status"] == "ok"),
                    len(rows),
                    len(new_rows),
                    json.dumps(details, ensure_ascii=True),
                    message,
                ),
            )

    def recent_runs(self, limit=25):
        rows = self.conn.execute(
            """SELECT id, started_at, finished_at, status, pages_checked,
                      listings_seen, new_count, details_json, message
               FROM scan_history ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        names = (
            "id", "started_at", "finished_at", "status", "pages_checked",
            "listings_seen", "new_count", "details_json", "message",
        )
        results = []
        for row in rows:
            item = dict(zip(names, row))
            item["details"] = json.loads(item.pop("details_json"))
            results.append(item)
        return results

    def close(self):
        self.conn.close()


def scan_once(session, state, sink, output_ids, url, pages, delay, on_progress=None):
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    blocks = scraper.BlockDetector()
    rows_by_id = {}
    page_results = []

    for page in range(1, pages + 1):
        if blocks.tripped.is_set():
            break
        page_url = scraper.page_url(url, page)
        if on_progress:
            on_progress({
                "phase": "fetching",
                "page": page,
                "pages": pages,
                "url": page_url,
                "started_at": started_at,
            })
        html = scraper.fetch(session, page_url, blocks=blocks)
        if not html:
            page_results.append({
                "page": page,
                "status": "failed",
                "url": page_url,
                "listing_count": 0,
                "listings": [],
            })
            continue
        page_rows = []
        for row in scraper.scrape_page(html):
            rows_by_id[row["id"]] = row
            page_rows.append(row)
        page_results.append({
            "page": page,
            "status": "ok",
            "url": page_url,
            "listing_count": len(page_rows),
            "listings": [
                {key: row.get(key, "") for key in ("id", "title", "year", "price_eur", "link")}
                for row in page_rows
            ],
        })
        if on_progress:
            on_progress({
                "phase": "page_complete",
                "page": page,
                "pages": pages,
                "listings_seen": len(rows_by_id),
                "page_listing_count": len(page_rows),
                "url": page_url,
                "started_at": started_at,
            })
        if page < pages:
            time.sleep(delay)

    observed_ids = set(rows_by_id)
    successful_pages = sum(1 for page in page_results if page["status"] == "ok")
    new_rows = []
    if successful_pages:
        if not state.is_primed():
            state.record_scan(observed_ids | output_ids, prime=True)
            message = "Initial baseline saved; existing listings were not exported."
            status = "baseline"
        else:
            known_ids = state.observed_ids() | output_ids
            new_rows = [row for ad_id, row in rows_by_id.items() if ad_id not in known_ids]
            sink.write(new_rows)
            state.record_scan(observed_ids | output_ids)
            output_ids.update(row["id"] for row in new_rows)
            message = f"Saved {len(new_rows)} newly discovered listing(s)."
            status = "ok"
    else:
        message = "No result pages were available; previous ID state was kept."
        status = "failed"

    state.record_run(started_at, status, page_results, list(rows_by_id.values()),
                     new_rows, message)
    if on_progress:
        on_progress({
            "phase": "complete",
            "started_at": started_at,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": status,
            "pages_checked": successful_pages,
            "listings_seen": len(observed_ids),
            "new_count": len(new_rows),
            "message": message,
        })
    print(message)
    if blocks.tripped.is_set():
        print("The site refused repeated requests; the watcher will back off "
              "for the rest of this interval.", file=sys.stderr)
    return {
        "status": status,
        "pages": page_results,
        "listings_seen": len(observed_ids),
        "new_rows": new_rows,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Poll recent Autoplius result pages and save newly discovered ads."
    )
    parser.add_argument("--url", default=scraper.DEFAULT_SEARCH,
                        help="search results URL to monitor")
    parser.add_argument("--pages", type=int, default=CFG["pages"],
                        help="latest result pages to check each cycle (default 3)")
    parser.add_argument("--interval-minutes", type=float, default=CFG["interval_minutes"],
                        help="minutes between checks (default 5)")
    parser.add_argument("--delay", type=float, default=CFG["delay"],
                        help="seconds between page requests (default 1)")
    parser.add_argument("--output", default=str(DATA_DIR / "new_listings.csv"),
                        help="CSV destination")
    parser.add_argument("--state", default=str(DATA_DIR / "new_listings_seen.sqlite"),
                        help="SQLite file holding observed ad IDs")
    args = parser.parse_args()
    if args.pages < 1:
        parser.error("--pages must be at least 1")
    if args.interval_minutes <= 0:
        parser.error("--interval-minutes must be greater than 0")
    if args.delay < 0:
        parser.error("--delay cannot be negative")

    state = SeenStore(args.state)
    output_ids = scraper.load_existing_ids(args.output)
    sink = scraper.CsvSink(args.output, scraper.FIELDS)
    interval = args.interval_minutes * 60

    print(f"Watching {args.url} ({args.pages} page(s), every "
          f"{args.interval_minutes:g} minute(s)).")
    print(f"New listings CSV: {Path(args.output).resolve()}")
    print("Listings are identified by new ad IDs between successful scans, "
          "not by an exact site-provided upload timestamp.")

    try:
        with requests.Session() as session:
            while True:
                print(f"\nScan started {datetime.now().astimezone().isoformat(timespec='seconds')}")
                scan_once(session, state, sink, output_ids, args.url,
                          args.pages, args.delay)
                print(f"Next scan in {args.interval_minutes:g} minute(s). "
                      "Press Ctrl+C to stop.")
                time.sleep(interval)
    except KeyboardInterrupt:
        print("\nWatcher stopped.")
    finally:
        sink.close()
        state.close()


if __name__ == "__main__":
    main()
