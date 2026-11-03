#!/usr/bin/env python3
"""
watch_new_listings.py — catch every new Autoplius ad within minutes.

Each scan walks the site's newest-first list ("Naujausi viršuje") page by page
and stops only once a WHOLE page holds nothing but ads it already knows, plus
one more page as a margin. So:

  * a quiet night costs two page requests; a busy hour simply reads further;
  * after downtime (PC off, site refusing) the next good scan walks back to
    where it left off, up to a safety cap (watcher.max_pages, ~a day of ads);
  * "raised" / promoted ads mixed into the list can't end a scan early,
    because one known ad on a page is never the stop signal, a fully known
    page is.

New ads go straight into data/autoplius.db with every card field, and are
priced at once with the saved pooled model (written by autoplius_stats.py).
A new car under the low end of its range has its ad page read immediately and
gets a verdict: deal / damaged / problem / not a real price. These land in the
new_deals table, which the dashboard shows.

Every scan is recorded in watch_scans, so a watcher that silently fails
shows up on the dashboard.

    python scripts/watch_new_listings.py              # run every 5 minutes
    python scripts/watch_new_listings.py --once       # one scan, then exit
    python scripts/watch_new_listings.py --verify 6   # are the last 6 h of ads all in the DB?
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

import autoplius_db as site
from autoplius_db import (BASE, DEFAULT_SEARCH, BlockDetector, Store, card_ages,
                          fetch, fetch_ad_details, normalized_label, page_url,
                          scrape_page)
from settings import SETTINGS, project_path

CFG = SETTINGS["watcher"]
SOURCE = "autoplius"
NEWEST_URL = f"{DEFAULT_SEARCH}?order_by=3&order_direction=DESC"

SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_scans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    status       TEXT,      -- ok / failed
    pages        INTEGER,
    seen         INTEGER,   -- distinct ads on the pages read
    new          INTEGER,   -- ads not in the database before this scan
    reached_known INTEGER,  -- 1 = stopped on a fully known page (no gap left)
    hit_cap      INTEGER,   -- 1 = stopped at max_pages: older ads may be missing
    oldest_age_min INTEGER, -- age of the oldest ad read, per its badge
    message      TEXT
);
CREATE INDEX IF NOT EXISTS idx_watch_scans_started ON watch_scans(started_at);

CREATE TABLE IF NOT EXISTS watch_seen (
    source       TEXT,
    id           TEXT,
    found_at     TEXT,
    published_at TEXT,      -- found_at minus the card's age badge, if it had one
    PRIMARY KEY (source, id)
);

CREATE TABLE IF NOT EXISTS new_deals (
    source   TEXT,
    id       TEXT,
    found_at TEXT,
    make     TEXT,
    model    TEXT,
    title    TEXT,
    year     INTEGER,
    mileage_km INTEGER,
    price    INTEGER,
    lo       INTEGER,
    mid      INTEGER,
    hi       INTEGER,
    verdict  TEXT,          -- deal / damaged / problem / not a real price / unchecked
    flags    TEXT,
    link     TEXT,
    PRIMARY KEY (source, id)
);
CREATE INDEX IF NOT EXISTS idx_new_deals_found ON new_deals(found_at);
"""

# Makes written with a space or hyphen in titles. Everything else is one word.
MULTIWORD_MAKES = ("mercedes-benz", "land-rover", "alfa-romeo", "aston-martin",
                   "rolls-royce", "great-wall", "lynk-co", "ds-automobiles")


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds")


slug = site.name_slug


# --------------------------------------------------------------------------- #
# make / model from a card title
# --------------------------------------------------------------------------- #
class ModelResolver:
    """
    Card titles ("Mercedes-Benz GLK320", "Volkswagen Caddy Maxi") to the
    (make, model) keys the database uses.

    First choice is the longest known make+model the title starts with, so a
    car lands in the same group as the full scrape put its neighbours. Ads of
    models the database has never seen still get stored: make from the title's
    first word (or a known multi-word make), model from the next word. Phase 2
    reconciles those with the site's catalogue.
    """

    def __init__(self, pairs):
        self.keys = sorted(((normalized_label(mk) + normalized_label(md), mk, md)
                            for mk, md in set(pairs) if mk and md),
                           key=lambda k: len(k[0]), reverse=True)
        self.makes = sorted({mk for mk, _ in pairs if mk} | set(MULTIWORD_MAKES),
                            key=lambda m: len(normalized_label(m)), reverse=True)

    def resolve(self, title):
        norm = normalized_label(title)
        if not norm:
            return None, None, False
        for key, mk, md in self.keys:
            # "bmw120" must not claim "BMW 1200": a number can't run on.
            if norm.startswith(key) and not (
                    key[-1].isdigit() and norm[len(key):len(key) + 1].isdigit()):
                return mk, md, True
        # Unknown model: the rest of the title is the model, the way the site
        # names its own ("tesla/model-3", not "tesla/model" for every Tesla).
        words = slug(title).split("-")
        for mk in self.makes:
            parts = mk.split("-")
            if words[:len(parts)] == parts:
                rest = words[len(parts):]
                return mk, ("-".join(rest[:3]) or None), False
        return words[0], ("-".join(words[1:4]) or None), False


# --------------------------------------------------------------------------- #
# the walk
# --------------------------------------------------------------------------- #
def walk(get_page, is_known, margin_pages=1, max_pages=50):
    """
    Read newest-first pages until a page holds only known ads, then
    `margin_pages` more. `get_page(n)` returns that page's HTML ("" = failed);
    `is_known(id)` says whether an ad is already stored.

    Returns {"rows": {id: row} of NEW ads in page order, "ages": {id: minutes},
    "seen": count, "pages": n, "reached_known", "hit_cap", "failed"}.
    """
    new, ages, seen = {}, {}, set()
    known_pages = 0
    result = {"rows": new, "ages": ages, "pages": 0, "reached_known": False,
              "hit_cap": False, "failed": False}
    for page in range(1, max_pages + 1):
        html = get_page(page)
        rows = list(scrape_page(html)) if html else []
        if not rows:                     # refused, empty or not a results page
            result["failed"] = True
            break
        result["pages"] = page
        ages.update(card_ages(html))
        fresh = 0
        for row in rows:
            seen.add(row["id"])
            if row["id"] in new or is_known(row["id"]):
                continue
            new[row["id"]] = row
            fresh += 1
        known_pages = known_pages + 1 if fresh == 0 else 0
        if known_pages > margin_pages:
            result["reached_known"] = True
            break
    else:
        result["hit_cap"] = True
    result["seen"] = len(seen)
    return result


# --------------------------------------------------------------------------- #
# verdicts
# --------------------------------------------------------------------------- #
def verdict(price, lo, mid, details=None, title=None, description=None, read=False):
    """
    What a new ad priced under its range really is.

      not a real price  under 30% of expected: lease figure, deposit, typo, parts
      damaged           a defect declared on the ad ("Defektai")
      problem           problem words in the seller's text (not running, no TA...)
      deal              nothing found after reading the ad
      unchecked         cheap, but the ad wasn't read
    """
    from autoplius_stats import IMPLAUSIBLE_SHARE, text_flags
    if mid and price < IMPLAUSIBLE_SHARE * mid:
        return "not a real price", []
    damage = (details or {}).get("damage_type")
    flags = ([damage] if damage else []) + text_flags(title, description)
    if damage:
        return "damaged", flags
    if flags:
        return "problem", flags
    return ("deal" if read else "unchecked"), flags


# --------------------------------------------------------------------------- #
# database side
# --------------------------------------------------------------------------- #
class WatchStore:
    def __init__(self, store):
        self.store = store
        with store.lock:
            store.conn.executescript(SCHEMA)
            store.conn.commit()

    def model_pairs(self):
        with self.store.lock:
            return self.store.conn.execute(
                """SELECT make, model FROM models
                   UNION SELECT DISTINCT make, model FROM listings""").fetchall()

    def is_known(self, ad_id):
        with self.store.lock:
            return self.store.conn.execute(
                """SELECT 1 FROM listings WHERE id = ?
                   UNION ALL SELECT 1 FROM watch_seen WHERE source = ? AND id = ?
                   LIMIT 1""", (ad_id, SOURCE, ad_id)).fetchone() is not None

    def mark_seen(self, rows, ages, found):
        with self.store.lock:
            self.store.conn.executemany(
                "INSERT OR IGNORE INTO watch_seen VALUES (?,?,?,?)",
                [(SOURCE, ad_id, iso(found),
                  iso(found - timedelta(minutes=ages[ad_id])) if ad_id in ages else None)
                 for ad_id in rows])
            self.store.conn.commit()

    def record_scan(self, started, result, new, message):
        oldest = max(result["ages"].values()) if result["ages"] else None
        with self.store.lock:
            self.store.conn.execute(
                """INSERT INTO watch_scans (source, started_at, finished_at, status,
                       pages, seen, new, reached_known, hit_cap, oldest_age_min, message)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (SOURCE, iso(started), iso(now_utc()),
                 "failed" if result["failed"] and not result["pages"] else "ok",
                 result["pages"], result.get("seen", 0), new,
                 int(result["reached_known"]), int(result["hit_cap"]), oldest, message))
            self.store.conn.commit()

    def record_deal(self, row, make, model, found, lo, mid, hi, verdict_, flags):
        with self.store.lock:
            self.store.conn.execute(
                """INSERT OR REPLACE INTO new_deals VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (SOURCE, row["id"], iso(found), make, model, row["title"],
                 row["year"] or None, row["mileage_km"] or None, row["price_eur"],
                 round(lo), round(mid), round(hi), verdict_, "|".join(flags), row["link"]))
            self.store.conn.commit()


def health(conn, source=SOURCE, stale_minutes=15):
    """Watcher health for the dashboard, from watch_scans / new_deals.
    `conn` is any sqlite3 connection to the main database."""
    try:
        last = conn.execute(
            """SELECT started_at, status, pages, new, reached_known, hit_cap, message
               FROM watch_scans WHERE source = ? ORDER BY id DESC LIMIT 1""",
            (source,)).fetchone()
        last_ok = conn.execute(
            """SELECT MAX(started_at) FROM watch_scans
               WHERE source = ? AND status = 'ok'""", (source,)).fetchone()[0]
        failures = 0
        for (status,) in conn.execute(
                "SELECT status FROM watch_scans WHERE source = ? ORDER BY id DESC LIMIT 50",
                (source,)):
            if status == "ok":
                break
            failures += 1
        day_ago = iso(now_utc() - timedelta(days=1))
        new_24h = conn.execute(
            "SELECT COALESCE(SUM(new), 0) FROM watch_scans WHERE source = ? AND started_at > ?",
            (source, day_ago)).fetchone()[0]
        deals = conn.execute(
            """SELECT id, found_at, make, model, title, year, mileage_km, price, lo, mid,
                      verdict, flags, link FROM new_deals
               WHERE source = ? ORDER BY found_at DESC LIMIT 30""", (source,)).fetchall()
    except Exception:          # tables not created yet: the watcher never ran
        return None
    stale = True
    if last_ok:
        age = (now_utc() - datetime.fromisoformat(last_ok)).total_seconds() / 60
        stale = age > stale_minutes
    keys = ("id", "found_at", "make", "model", "title", "year", "km", "price", "lo",
            "mid", "verdict", "flags", "link")
    return {
        "last": dict(zip(("started_at", "status", "pages", "new", "reached_known",
                          "hit_cap", "message"), last)) if last else None,
        "last_ok": last_ok, "failures": failures, "stale": stale, "new_24h": new_24h,
        "deals": [dict(zip(keys, d)) for d in deals],
    }


# --------------------------------------------------------------------------- #
# one scan
# --------------------------------------------------------------------------- #
def load_scoring():
    """The saved price model and generation table, or (None, None)."""
    try:
        import autoplius_stats as stats
    except ImportError:
        return None, None
    model = stats.PriceModel.load(stats.DATA_DIR / "price_model")
    compiled = stats.compile_generations(
        stats.ensure_generations(stats.DATA_DIR / "generations.json"))
    return model, compiled


def scan(session, store, watch, model, compiled, args):
    started = now_utc()
    blocks = BlockDetector(threshold=3)
    resolver = ModelResolver(watch.model_pairs())

    def get_page(n):
        if n > 1:
            time.sleep(args.delay)
        return fetch(session, page_url(NEWEST_URL, n), blocks=blocks)

    result = walk(get_page, watch.is_known, args.margin_pages, args.max_pages)
    new = result["rows"]
    found = now_utc()

    rows, guessed = [], 0
    for row in new.values():
        make, model_name, known_model = resolver.resolve(row["title"])
        if not make or not model_name:
            continue
        guessed += not known_model
        rows.append({**row, "_make": make, "_model": model_name})
    store.upsert_many(rows, None, None)
    watch.mark_seen(new, result["ages"], found)

    deals = score_new(session, store, watch, rows, model, compiled, found, blocks, args)

    if result["failed"] and not result["pages"]:
        message = "No results page could be read; nothing changed."
    else:
        message = (f"{len(new)} new ad(s) on {result['pages']} page(s)"
                   + (f", {guessed} under a model not in the model list" if guessed else "")
                   + (f", {deals} below their price range" if deals else ""))
        if result["hit_cap"]:
            message += (f". Stopped at the {args.max_pages}-page cap: ads older than that "
                        "may be missing; 'Resume scraping' fills the rest.")
        elif result["failed"]:
            message += ". A later page failed; the next scan picks up from there."
    watch.record_scan(started, result, len(new), message)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)
    if blocks.tripped.is_set():
        print("  ! Autoplius refused repeated requests this scan.", file=sys.stderr)
    return result


def score_new(session, store, watch, rows, model, compiled, found, blocks, args):
    """Price new ads; read the ad of each one under its range; record verdicts."""
    if model is None or not rows:
        return 0
    from autoplius_stats import IMPLAUSIBLE_SHARE, generation_for
    cheap = 0
    for row in rows:
        if row["price_eur"] == "" or not row["year"]:
            continue
        car = {"make": row["_make"], "model": row["_model"], "year": row["year"],
               "mileage_km": row["mileage_km"] or None, "fuel": row["fuel"],
               "engine_l": row["engine_l"], "power_kw": row["power_kw"],
               "gearbox": row["gearbox"], "body": row["body"], "dealer": row["dealer"],
               "gen": generation_for(row["_make"], row["_model"], row["year"], compiled)}
        if not model.knows(car):          # never trained on this model: no verdict
            continue
        lo, mid, hi = model.price(car)
        if row["price_eur"] >= lo:
            continue
        cheap += 1
        details, description, read = {}, None, False
        if args.read_cheap_ads and mid and row["price_eur"] >= IMPLAUSIBLE_SHARE * mid \
                and not blocks.tripped.is_set():
            time.sleep(args.delay)
            description, details, read, _ = fetch_ad_details(session, row["link"], blocks)
            if read:
                store.save_description(row["id"], description, details)
        v, flags = verdict(row["price_eur"], lo, mid, details, row["title"], description, read)
        watch.record_deal(row, row["_make"], row["_model"], found, lo, mid, hi, v, flags)
        print(f"  {v.upper():<16} {row['title']} {row['year']} €{row['price_eur']:,} "
              f"(expected €{mid:,.0f}, low end €{lo:,.0f}) {row['link']}", flush=True)
    return cheap


# --------------------------------------------------------------------------- #
# verify: are the last N hours really all in the database?
# --------------------------------------------------------------------------- #
def verify(session, store, watch, hours, delay):
    """Walk the newest-first list back `hours` (by the cards' age badges) and
    list every ad that is not in the database. Read-only."""
    limit, checked, missing = hours * 60, 0, []
    blocks = BlockDetector(threshold=3)
    for page in range(1, 500):
        html = fetch(session, page_url(NEWEST_URL, page), blocks=blocks)
        rows = list(scrape_page(html)) if html else []
        if not rows:
            print("Could not read page", page, "- result is partial.")
            break
        ages = card_ages(html)
        for row in rows:
            age = ages.get(row["id"])
            if age is not None and age > limit:
                continue
            checked += 1
            if not watch.is_known(row["id"]):
                missing.append((age, row["title"], row["link"]))
        # Past the window, or past the ads that still carry an age badge.
        if not ages or min(ages.values()) > limit:
            break
        time.sleep(delay)
    print(f"{checked} ads published in the last {hours} h; "
          f"{checked - len(missing)} in the database, {len(missing)} missing.")
    for age, title, link in missing:
        print(f"  missing: {title} ({age} min old) {link}")
    return missing


def main():
    ap = argparse.ArgumentParser(
        description="Catch every new Autoplius ad and store it in the database.")
    ap.add_argument("--db", default=str(project_path(SETTINGS["paths"]["database"])))
    ap.add_argument("--interval-minutes", type=float, default=CFG["interval_minutes"])
    ap.add_argument("--margin-pages", type=int, default=CFG["margin_pages"])
    ap.add_argument("--max-pages", type=int, default=CFG["max_pages"])
    ap.add_argument("--delay", type=float, default=CFG["delay"])
    ap.add_argument("--no-read", dest="read_cheap_ads", action="store_false",
                    default=CFG["read_cheap_ads"],
                    help="don't open the ads of cheap new cars")
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

    store = Store(args.db)
    watch = WatchStore(store)
    session = requests.Session()
    try:
        if args.verify:
            verify(session, store, watch, args.verify, args.delay)
            return
        model, compiled = load_scoring()
        model_mtime = _model_mtime()
        print(f"Watching {NEWEST_URL} every {args.interval_minutes:g} min "
              f"(stop after {args.margin_pages + 1} fully known page(s), "
              f"cap {args.max_pages} pages).")
        print("New ads are priced " + ("with the saved model." if model else
              "- no saved model yet: run autoplius_stats.py once to enable deal checks."))
        while True:
            started = time.monotonic()
            if _model_mtime() != model_mtime:      # report rebuilt: pick up the new model
                model, compiled = load_scoring()
                model_mtime = _model_mtime()
            try:
                scan(session, store, watch, model, compiled, args)
            except requests.RequestException as exc:
                print(f"  ! scan failed: {exc}", file=sys.stderr)
            if args.once:
                break
            wait = args.interval_minutes * 60 - (time.monotonic() - started)
            time.sleep(max(5.0, wait))
    except KeyboardInterrupt:
        print("\nWatcher stopped.")
    finally:
        session.close()
        store.close()


def _model_mtime():
    try:
        return (site.DATA_DIR / "price_model" / "meta.json").stat().st_mtime
    except OSError:
        return None


if __name__ == "__main__":
    main()
