#!/usr/bin/env python3
"""
scrape.py — build and refresh the listings database, site by site.

For each enabled site (config.toml) it syncs the site's make/model catalogue
(weekly), scrapes every model that is due, model by model and resumably, then
sweeps the site's damaged listings. Listings, price changes and models are
stored per site in data/autoplius.db.

    python scripts/scrape.py                    # everything due, every enabled site
    python scripts/scrape.py --site autoplius   # one site
    python scripts/scrape.py --limit 50         # at most 50 models this run
    python scripts/scrape.py --force            # every model, however fresh
    python scripts/scrape.py --models "bmw/320,audi/a4"
    python scripts/scrape.py --damage-sweep     # only the damaged-listings sweep
    python scripts/scrape.py --backfill-descriptions --ids-file data/candidates.txt
    python scripts/scrape.py --coverage         # site counts vs database
    python scripts/scrape.py --status | --export cars.csv | --prune-gone [--dry-run]

Re-run any time: models already scraped are skipped until they're older than
autoplius.max_age_days (models with no ads: empty_max_age_days).
"""

import argparse
import sys
import time

import requests

from collectors import crawl, enabled_sites, get_site
from collectors.common import (STOP, BlockDetector, diagnose, install_sigint_handler,
                               load_ids_file, load_models_file)
from collectors.store import Store, do_export, do_prune_gone, do_status
from settings import SETTINGS, project_path

CFG = SETTINGS["autoplius"]


def parse_args():
    ap = argparse.ArgumentParser(description="Build the listings database")
    ap.add_argument("--site", action="append", default=None,
                    help="only this site (repeatable); default: every enabled site")
    ap.add_argument("--db", default=str(project_path(SETTINGS["paths"]["database"])))
    ap.add_argument("--concurrency", type=int, default=CFG["concurrency"])
    ap.add_argument("--delay", type=float, default=CFG["delay"])
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--cap-threshold", type=int, default=3900,
                    help="a search returning this many is assumed capped")
    ap.add_argument("--min-width", type=int, default=50)
    ap.add_argument("--limit", type=int, default=None,
                    help="scrape at most this many models per site this run")
    ap.add_argument("--max-age", type=float, default=CFG["max_age_days"],
                    help="re-scrape a model if older than this many days")
    ap.add_argument("--empty-max-age", type=float, default=CFG["empty_max_age_days"],
                    help="re-check models that had no listings after this many days")
    ap.add_argument("--force", action="store_true", help="re-scrape every model")
    ap.add_argument("--models", default=None, help='only these, e.g. "bmw/320,audi/a4"')
    ap.add_argument("--models-file", default=None,
                    help="only the 'make/model' pairs listed in this file")
    ap.add_argument("--refresh-models", action="store_true",
                    help="re-fetch the make/model catalogue now (also done weekly)")
    ap.add_argument("--damage-sweep", action="store_true",
                    help="only run the damaged-listings sweep")
    ap.add_argument("--no-damage-sweep", action="store_true",
                    help="skip the damage sweep that follows a scrape")
    ap.add_argument("--block-threshold", type=int, default=CFG["block_threshold"])
    ap.add_argument("--backfill-descriptions", action="store_true",
                    help="fetch seller descriptions for listings not checked yet")
    ap.add_argument("--ids-file", default=None,
                    help="with --backfill-descriptions: only these ads, in order "
                         "(lines 'site:id' or 'id'; autoplius_stats.py writes "
                         "data/candidates.txt)")
    ap.add_argument("--description-delay", type=float, default=CFG["description_delay"])
    ap.add_argument("--description-limit", type=int, default=None)
    ap.add_argument("--all-descriptions", action="store_true",
                    help="after scraping, fetch every missing description (slow)")
    ap.add_argument("--status", action="store_true", help="show progress and exit")
    ap.add_argument("--coverage", action="store_true",
                    help="compare the site's per-make counts with the database")
    ap.add_argument("--diagnose", action="store_true",
                    help="check what the site will serve this machine")
    ap.add_argument("--export", default=None, help="export listings to CSV and exit")
    ap.add_argument("--prune-gone", action="store_true",
                    help="delete listings their model's latest scrape didn't find")
    ap.add_argument("--dry-run", action="store_true", help="with --prune-gone: only report")
    args = ap.parse_args()
    if args.description_delay < 0:
        ap.error("--description-delay must be zero or greater")
    if args.description_limit is not None and args.description_limit < 1:
        ap.error("--description-limit must be at least 1")
    return args


def ids_for(site, entries):
    """Ids from an ids file that belong to `site`: 'autoplius:123' or bare '123'."""
    out = []
    for entry in entries:
        source, _, ad_id = entry.rpartition(":")
        if not source or source == site.SOURCE:
            out.append(ad_id)
    return out


def sync_catalogue(site, db, session, blocks, args):
    age = db.catalogue_age_days()
    if not (args.refresh_models or db.models_missing_ids() or age is None or age > 7):
        return True
    entries = site.fetch_catalogue(session, blocks)
    if not entries:
        print(f"\nCould not read {site.SOURCE}'s make/model catalogue.", file=sys.stderr)
        print("Run:  python scripts/scrape.py --diagnose", file=sys.stderr)
        return age is not None          # an older catalogue is still usable
    updated, added = db.sync_catalogue(entries, site.catalogue_url)
    print(f"Catalogue: {len(entries)} models, {updated} already known, {added} added\n")
    return True


def run_site(site, store, session, args):
    db = store.site(site.SOURCE)
    blocks = BlockDetector(threshold=args.block_threshold)

    if args.backfill_descriptions:
        ids = ids_for(site, load_ids_file(args.ids_file)) if args.ids_file else None
        crawl.fetch_missing_descriptions(site, db, session, BlockDetector(threshold=1),
                                         args.description_delay, args.description_limit, ids)
        return blocks
    if args.diagnose:
        crawl.do_diagnose(site, session)
        return blocks
    if args.coverage:
        crawl.do_coverage(site, store, session)
        return blocks
    if not sync_catalogue(site, db, session, blocks, args):
        return blocks
    if args.damage_sweep:
        crawl.damage_sweep(site, db, session, blocks, args)
        return blocks

    wanted = None
    if args.models:
        wanted = {m.strip().lower() for m in args.models.split(",") if m.strip()}
    if args.models_file:
        wanted = (wanted or set()) | load_models_file(args.models_file)
    if wanted is not None:
        todo = [(u, mk, md) for (u, mk, md) in db.pending_models(0, True)
                if f"{mk}/{md}".lower() in wanted]
    else:
        todo = db.pending_models(args.max_age, args.force, args.empty_max_age)
    if args.limit is not None:     # 0 legitimately means "sync the catalogue, scrape nothing"
        todo = todo[:args.limit]

    print(f"{site.SOURCE}: {len(todo)} model(s) to scrape\n" if todo else
          f"{site.SOURCE}: nothing to scrape — every model is up to date.")
    for i, (url, make, model) in enumerate(todo, start=1):
        if STOP.is_set() or blocks.tripped.is_set():
            break
        print(f"[{i}/{len(todo)}] {make}/{model}")
        crawl.scrape_model(site, url, make, model, db, session, blocks, args)
        time.sleep(args.delay)       # most models are one page: keep the pace polite
    if todo and wanted is None and not args.no_damage_sweep \
            and not STOP.is_set() and not blocks.tripped.is_set():
        crawl.damage_sweep(site, db, session, blocks, args)
    if args.all_descriptions and not STOP.is_set() and not blocks.tripped.is_set():
        crawl.fetch_missing_descriptions(site, db, session, blocks, args.description_delay,
                                         args.description_limit)
    elif todo:
        print("Descriptions are read for candidates only: run autoplius_stats.py, then\n"
              "  scrape.py --backfill-descriptions --ids-file data/candidates.txt")
    return blocks


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    args = parse_args()
    install_sigint_handler()
    store = Store(args.db)
    if args.status or args.export or args.prune_gone:
        if args.status:
            do_status(store)
        if args.export:
            do_export(store, args.export)
        if args.prune_gone:
            do_prune_gone(store, args.dry_run)
        store.close()
        return

    sites = [get_site(s) for s in args.site] if args.site else enabled_sites()
    session = requests.Session()
    refused = []
    try:
        for site in sites:
            blocks = run_site(site, store, session, args)
            if blocks.tripped.is_set():
                refused.append((site, blocks))
            if STOP.is_set():
                break
    except KeyboardInterrupt:
        STOP.set()
    finally:
        total, done, mtotal = store.counts()
        print("=" * 68)
        print(f"New listings inserted : {store.inserted}")
        print(f"Existing refreshed    : {store.updated}")
        print(f"Price changes logged  : {store.price_changes}")
        print(f"Database total        : {total} listings")
        print(f"Models scraped        : {done} / {mtotal} enabled")
        print("=" * 68)
        for site, blocks in refused:
            print(f"\n{site.SOURCE} was refusing requests when we stopped.", file=sys.stderr)
            if blocks.dump_path.exists():
                body = blocks.dump_path.read_text(encoding="utf-8", errors="replace")
                print(f"Looks like: {diagnose(body)}", file=sys.stderr)
            print("Wait a while, then re-run — finished models are skipped.", file=sys.stderr)
        store.close()
        session.close()


if __name__ == "__main__":
    main()
