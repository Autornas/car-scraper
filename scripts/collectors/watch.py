"""
collectors/watch.py — catch every new ad on a site within minutes.

Each scan walks the site's newest-first list page by page and stops only once
a WHOLE page holds nothing but ads it already knows, plus `margin_pages` more:

  * a quiet period costs two page requests; a busy hour simply reads further;
  * after downtime (PC off, site refusing) the next good scan walks back to
    where it left off, up to a safety cap (watcher.max_pages, ~a day of ads);
  * "raised" / promoted ads mixed into the list can't end a scan early,
    because one known ad on a page is never the stop signal, a fully known
    page is.

New ads go straight into the database with every card field, and are priced
at once with the saved pooled model (written by autoplius_stats.py). A new car
under the low end of its range is then compared with the similar cars already
in the database (same model, year +-1, mileage +-30%): the price model alone
called cars cheap that cost more than their neighbours. One that isn't cheap
next to them is dropped; the rest have their ad page read and get a verdict:
deal / maybe (too few similar cars to confirm) / damaged / problem / not a
real price, in the new_deals table.
Every scan is recorded in watch_scans, so a watcher that silently fails shows
up on the dashboard.

`site` is a site module (collectors.autoplius); see scripts/watch.py.
"""

import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

from collectors.common import (DATA_DIR, BlockDetector, fetch, name_slug, normalized_label,
                               refusal_kind)
from collectors import alerts, watchlist
from collectors.dupes import same_car_elsewhere
from collectors.verdicts import (IMPLAUSIBLE_SHARE, TOO_CHEAP_FLAG, classify, country_flags,
                                 text_flags, too_cheap)
from collectors.verdicts import SCHEMA as LABELS_SCHEMA

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
    verdict  TEXT,          -- deal / maybe / damaged / problem / not a real price / unchecked
    flags    TEXT,
    link     TEXT,
    also     TEXT,          -- the same car on another site: "site|price|link"
    similar_n       INTEGER,   -- similar cars in the database when it was found
    similar_cheaper INTEGER,   -- how many of them cost less than this one
    similar_median  INTEGER,   -- their median asking price
    watch    INTEGER,       -- 1 = fits your watchlist, 0 = doesn't, NULL = no watchlist set
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


slug = name_slug


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


def walk(site, get_page, is_known, margin_pages=1, max_pages=50):
    """
    Read newest-first pages until a page holds only known ads, then
    `margin_pages` more. `get_page(n)` returns that page's HTML ("" = failed);
    `is_known(id)` says whether an ad is already stored; `site` parses pages.

    Returns {"rows": {id: row} of NEW ads in page order, "ages": {id: minutes},
    "seen": count, "pages": n, "reached_known", "hit_cap", "failed"}.
    """
    new, ages, seen = {}, {}, set()
    known_pages = 0
    result = {"rows": new, "ages": ages, "pages": 0, "reached_known": False,
              "hit_cap": False, "failed": False}
    sorted_newest = getattr(site, "is_newest_first", None)
    for page in range(1, max_pages + 1):
        html = get_page(page)
        if page == 1 and html and sorted_newest and sorted_newest(html) is False:
            # The site answered with another sort order. Walking it would find
            # nothing new and report a healthy scan, so fail loudly instead.
            result["failed"] = result["wrong_sort"] = True
            break
        rows = list(site.scrape_page(html)) if html else []
        if not rows:                     # refused, empty or not a results page
            result["failed"] = True
            break
        result["pages"] = page
        ages.update(site.card_ages(html))
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


def verdict(price, lo, mid, details=None, title=None, description=None, read=False,
            similar=None, min_similar=5):
    """
    What a new ad priced under its range really is.

      not a real price  under 30% of expected: lease figure, deposit, typo, parts.
                        Or the ad says so itself (an auction bid, a price without
                        VAT, a down payment), or the car costs under half of
                        what enough similar cars ask and nothing explains it
      damaged           a defect declared on the ad ("Defektai")
      problem           problem words in the seller's text (not running, no TA...),
                        a US import or a UK (right-hand drive) car
      deal              nothing found after reading the ad
      maybe             the same, but too few similar cars to confirm it
      unchecked         cheap, but the ad wasn't read

    `details`: the ad page's fields (damage_type, reg_country); `similar`:
    (similar cars in the database, how many are cheaper, their median) from
    market_check, or None to judge without them.
    """
    if mid and price < IMPLAUSIBLE_SHARE * mid:
        return "not a real price", []
    details = details or {}
    damage = details.get("damage_type")
    flags = list(dict.fromkeys(([damage] if damage else []) + text_flags(title, description)
                               + country_flags(details.get("reg_country"))))
    # Nothing in the ad explains it, yet it costs half of its neighbours.
    if not flags and similar and too_cheap(price, similar[0], similar[2], min_similar):
        flags.append(TOO_CHEAP_FLAG)
    # the same rules the report uses (collectors.verdicts.classify)
    result = classify(price, lo, mid, bool(damage), flags, read) or "unchecked"
    if result == "deal" and similar is not None and (similar[0] or 0) < min_similar:
        result = "maybe"                   # too few similar cars to confirm the model
    return result, flags


# --------------------------------------------------------------------------- #


# is it cheap next to similar cars?


# --------------------------------------------------------------------------- #


def similar_prices(conn, source, ad_id, car, year_span=1, km_span=0.3, min_n=5):
    """Asking prices of the cars most like this one already in the database:
    same make/model, year within `year_span`, mileage within `km_span` (when
    this car's is known), no declared defect. Same fuel only, when that still
    leaves `min_n` cars."""
    query = """SELECT price_eur, fuel FROM listings
               WHERE make = ? AND model = ? AND year BETWEEN ? AND ?
                 AND price_eur > 0 AND COALESCE(damaged, 0) = 0 AND damage_type IS NULL
                 AND NOT (source = ? AND id = ?)"""
    params = [car["make"], car["model"], car["year"] - year_span, car["year"] + year_span,
              source, ad_id]
    km = car.get("mileage_km")
    if km:
        query += " AND mileage_km BETWEEN ? AND ?"
        params += [km * (1 - km_span), km * (1 + km_span)]
    rows = conn.execute(query, params).fetchall()
    same_fuel = [p for p, fuel in rows if car.get("fuel") and fuel == car["fuel"]]
    return same_fuel if len(same_fuel) >= min_n else [p for p, _ in rows]


def market_check(price, similar, min_n=5, min_discount=0.10, cheapest_share=0.25):
    """
    Is `price` really cheap next to `similar` (asking prices of similar cars)?

      "cheap"      among the cheapest quarter and at least `min_discount`
                   under their median
      "not cheap"  enough similar cars, and it isn't
      "few"        fewer than `min_n` similar cars: can't tell

    Returns (state, n, cheaper, median): how many there are, how many cost
    less than this one, and their median (None with no similar car).
    """
    n = len(similar)
    cheaper = sum(1 for p in similar if p < price)
    median = round(statistics.median(similar)) if similar else None
    if n < min_n:
        return "few", n, cheaper, median
    cheap = cheaper <= cheapest_share * n and price <= (1 - min_discount) * median
    return ("cheap" if cheap else "not cheap"), n, cheaper, median


# --------------------------------------------------------------------------- #


# database side


# --------------------------------------------------------------------------- #


def ensure_tables(conn):
    """Create the watcher's tables, and bring older ones up to date: new_deals
    gained `also` in Phase 5, and the deals list joins `labels` (Phase 6)."""
    conn.executescript(SCHEMA)
    conn.executescript(LABELS_SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(new_deals)")}
    for name, kind in (("also", "TEXT"), ("similar_n", "INTEGER"),
                       ("similar_cheaper", "INTEGER"), ("similar_median", "INTEGER"),
                       ("watch", "INTEGER")):
        if name not in have:
            conn.execute(f"ALTER TABLE new_deals ADD COLUMN {name} {kind}")
    conn.commit()


class WatchStore:
    """The watcher's tables (watch_scans, watch_seen, new_deals) for one site."""

    def __init__(self, store, source):
        self.store, self.source = store, source
        self.db = store.site(source)
        with store.lock:
            ensure_tables(store.conn)

    def model_pairs(self):
        return self.db.canonical_pairs()

    def is_known(self, ad_id):
        with self.store.lock:
            return self.store.conn.execute(
                """SELECT 1 FROM listings WHERE source = ? AND id = ?
                   UNION ALL SELECT 1 FROM watch_seen WHERE source = ? AND id = ?
                   LIMIT 1""", (self.source, ad_id, self.source, ad_id)).fetchone() is not None

    def mark_seen(self, rows, ages, found):
        with self.store.lock:
            self.store.conn.executemany(
                "INSERT OR IGNORE INTO watch_seen VALUES (?,?,?,?)",
                [(self.source, ad_id, iso(found),
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
                (self.source, iso(started), iso(now_utc()),
                 "failed" if result["failed"] and not result["pages"] else "ok",
                 result["pages"], result.get("seen", 0), new,
                 int(result["reached_known"]), int(result["hit_cap"]), oldest, message))
            self.store.conn.commit()

    def record_deal(self, row, make, model, found, lo, mid, hi, verdict_, flags, also=None,
                    similar=(None, None, None), watch=None):
        """`similar`: (how many similar cars, how many are cheaper, their median);
        `watch`: whether it fits the watchlist (None = no watchlist set)."""
        also_text = f"{also['source']}|{also['price_eur']}|{also['link']}" if also else None
        with self.store.lock:
            self.store.conn.execute(
                """INSERT OR REPLACE INTO new_deals
                   (source, id, found_at, make, model, title, year, mileage_km, price,
                    lo, mid, hi, verdict, flags, link, also,
                    similar_n, similar_cheaper, similar_median, watch)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.source, row["id"], iso(found), make, model, row["title"],
                 row["year"] or None, row["mileage_km"] or None, row["price_eur"],
                 round(lo), round(mid), round(hi), verdict_, "|".join(flags), row["link"],
                 also_text, *similar, None if watch is None else int(watch)))
            self.store.conn.commit()

    def watchlist(self):
        with self.store.lock:
            return watchlist.load(self.store.conn)

    def similar_prices(self, ad_id, car, min_n):
        with self.store.lock:
            return similar_prices(self.store.conn, self.source, ad_id, car, min_n=min_n)

    def same_car_elsewhere(self, car):
        with self.store.lock:
            return same_car_elsewhere(self.store.conn, self.source, car)


def health(conn, source, stale_minutes=15):
    """Watcher health for the dashboard, from watch_scans / new_deals.
    `conn` is any sqlite3 connection to the main database. None if the
    watcher has never run here."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "watch_scans" not in tables:
        return None
    try:
        ensure_tables(conn)          # a database last touched by an older watcher
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
        select = """SELECT d.id, d.found_at, d.make, d.model, d.title, d.year, d.mileage_km,
                           d.price, d.lo, d.mid, d.verdict, d.flags, d.link, d.also, l.label,
                           d.similar_n, d.similar_cheaper, d.similar_median, d.watch
                    FROM new_deals d LEFT JOIN labels l ON l.source = d.source AND l.id = d.id
                    WHERE d.source = ?"""
        deals = conn.execute(select + " ORDER BY d.found_at DESC LIMIT 30", (source,)).fetchall()
        # the ones that fit the watchlist, further back than the recent 30
        watched = conn.execute(
            select + " AND d.watch = 1 ORDER BY d.found_at DESC LIMIT 30", (source,)).fetchall()
    except sqlite3.Error as exc:
        # Reported, not hidden: "never run" for a real error is how the
        # Phase 5 column change went unnoticed on the dashboard.
        print(f"Watcher health for {source} failed: {exc}", file=sys.stderr)
        return None
    stale = True
    if last_ok:
        age = (now_utc() - datetime.fromisoformat(last_ok)).total_seconds() / 60
        stale = age > stale_minutes
    keys = ("id", "found_at", "make", "model", "title", "year", "km", "price", "lo",
            "mid", "verdict", "flags", "link", "also", "label",
            "similar_n", "similar_cheaper", "similar_median", "watch")
    deals = [{**dict(zip(keys, d)), "source": source} for d in deals]
    watched = [{**dict(zip(keys, d)), "source": source} for d in watched]
    return {
        "last": dict(zip(("started_at", "status", "pages", "new", "reached_known",
                          "hit_cap", "message"), last)) if last else None,
        "last_ok": last_ok, "failures": failures, "stale": stale, "new_24h": new_24h,
        "deals": deals, "watch_deals": watched,
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


def scan(site, session, store, watch, model, compiled, args):
    started = now_utc()
    blocks = BlockDetector(threshold=3)
    resolver = ModelResolver(watch.model_pairs())

    last_html = [""]

    def get_page(n):
        if n > 1:
            time.sleep(args.delay)
        last_html[0] = fetch(session, site.page_url(site.newest_url, n), blocks=blocks)
        return last_html[0]

    result = walk(site, get_page, watch.is_known, args.margin_pages, args.max_pages)
    new = result["rows"]
    found = now_utc()

    rows, guessed = [], 0
    for row in new.values():
        make, model_name, known_model = resolver.resolve(row["title"])
        if not make or not model_name:
            continue
        guessed += not known_model
        rows.append({**row, "_make": make, "_model": model_name})
    watch.db.upsert_many(rows, None, None)
    watch.mark_seen(new, result["ages"], found)

    deals = score_new(site, session, watch, rows, model, compiled, found, blocks, args)

    if result.get("wrong_sort"):
        message = ("The site's list is not sorted newest-first any more (its sort "
                   "option changed); nothing stored. The site module's newest_url "
                   "needs updating.")
    elif result["failed"] and not result["pages"]:
        # Name the refusal: "HTTP 403, a Cloudflare browser challenge" tells you
        # whether waiting will help; "nothing could be read" doesn't.
        if blocks.first_status:
            why = f"HTTP {blocks.first_status}" + (
                f", {blocks.first_reason}" if blocks.first_reason else "")
        elif last_html[0]:
            why = f"the page was not a results list: {refusal_kind(last_html[0])}"
        else:
            why = "no answer from the site"
        message = f"No results page could be read ({why}); nothing changed."
    else:
        message = (f"{len(new)} new ad(s) on {result['pages']} page(s)"
                   + (f", {guessed} under a model not in the model list" if guessed else "")
                   + (f", {deals} below their price range" if deals else ""))
        if result["hit_cap"]:
            message += (f". Stopped at the {args.max_pages}-page cap: ads older than that "
                        "may be missing; 'Update database' fills the rest.")
        elif result["failed"]:
            message += ". A later page failed; the next scan picks up from there."
    watch.record_scan(started, result, len(new), message)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)
    if blocks.tripped.is_set():
        print(f"  ! {site.SOURCE} refused repeated requests this scan.", file=sys.stderr)
    return result


def score_new(site, session, watch, rows, model, compiled, found, blocks, args):
    """Price new ads; read the ad of each one under its range; record verdicts."""
    if model is None or not rows:
        return 0
    from autoplius_stats import generation_for, implausibly_low_km
    cheap = 0
    min_n, min_discount = getattr(args, "min_similar", 5), getattr(args, "min_discount", 0.10)
    wanted = watch.watchlist()             # read every scan: edits apply at once
    for row in rows:
        if row["price_eur"] == "" or not row["year"]:
            continue
        make, model_name = watch.db.canonical(row["_make"], row["_model"])
        km = row["mileage_km"] or None
        if km and implausibly_low_km(km, max(1, found.year - row["year"])):
            km = None                      # "326 km" on a 2004 car: priced as unknown
        car = {"source": site.SOURCE, "make": make, "model": model_name, "year": row["year"],
               "mileage_km": km, "fuel": row["fuel"],
               "engine_l": row["engine_l"], "power_kw": row["power_kw"],
               "gearbox": row["gearbox"], "body": row["body"], "dealer": row["dealer"],
               "gen": generation_for(make, model_name, row["year"], compiled),
               "price_eur": row["price_eur"], "location": row["location"]}
        if not model.knows(car):          # learned from too few cars of it: no verdict
            continue
        lo, mid, hi = model.price(car)
        if row["price_eur"] >= lo:
            continue
        # The model says cheap. Do the similar cars in the database agree?
        state, *similar = market_check(
            row["price_eur"], watch.similar_prices(row["id"], car, min_n), min_n, min_discount)
        if state == "not cheap":
            print(f"  not cheap after all: {row['title']} {row['year']} €{row['price_eur']:,} "
                  f"- {similar[1]} of {similar[0]} similar cars cost less "
                  f"(their median €{similar[2]:,})", flush=True)
            continue
        cheap += 1
        details, description, read = {}, None, False
        if args.read_cheap_ads and mid and row["price_eur"] >= IMPLAUSIBLE_SHARE * mid \
                and not blocks.tripped.is_set():
            time.sleep(args.delay)
            description, details, read, _ = site.fetch_ad_details(session, row["link"], blocks)
            if read:
                watch.db.save_description(row["id"], description, details)
        v, flags = verdict(row["price_eur"], lo, mid, details, row["title"], description, read,
                           similar, min_n)
        also = watch.same_car_elsewhere(car)
        watched = None
        if watchlist.is_set(wanted):
            watched = watchlist.matches(
                wanted, make, model_name, row["price_eur"], row["year"], row["mileage_km"] or None,
                watchlist.discount(row["price_eur"], mid, similar[0], similar[2], min_n))
        watch.record_deal(row, make, model_name, found, lo, mid, hi, v, flags, also, similar,
                          watched)
        if watchlist.should_alert(wanted, v, watched):
            alerts.deal_alert(site.SOURCE, row["title"], row["year"], row["price_eur"],
                              mid, lo, row["link"], also, similar, v)
        print(f"  {v.upper():<16} {'★ ' if watched else ''}"
              f"{row['title']} {row['year']} €{row['price_eur']:,} "
              f"(typical asking €{mid:,.0f}, low end €{lo:,.0f}; "
              + (f"{similar[1]} of {similar[0]} similar cars cheaper, median €{similar[2]:,}"
                 if similar[0] else "no similar cars in the database")
              + f") {row['link']}"
              + (f"  [{', '.join(flags)}]" if flags else "")
              + (f"  [also on {also['source']} €{also['price_eur']:,}]" if also else ""),
              flush=True)
    return cheap


def recheck_deals(conn, min_n=5, min_discount=0.10):
    """
    Judge the cars already in new_deals again with today's rules and today's
    database: the word lists grow, and a car that had 2 similar cars last week
    may have 20 now. Nothing is fetched; it works from what is stored. A car
    that turns out not to be cheap next to its neighbours becomes "not cheap".
    Returns how many verdicts changed.
    """
    ensure_tables(conn)
    rows = conn.execute(
        """SELECT d.source, d.id, d.price, d.lo, d.mid, d.verdict, d.flags, d.title, d.make,
                  d.model, d.year, d.mileage_km, l.description, l.description_checked_at,
                  l.damage_type, l.reg_country, l.fuel
           FROM new_deals d LEFT JOIN listings l ON l.source = d.source AND l.id = d.id""").fetchall()
    wanted = watchlist.load(conn)
    changed = 0
    for (source, ad_id, price, lo, mid, old, old_flags, title, make, model, year, km,
         description, checked, damage, country, fuel) in rows:
        if not price or not mid or not year:
            continue
        car = {"make": make, "model": model, "year": year, "mileage_km": km, "fuel": fuel}
        state, *similar = market_check(
            price, similar_prices(conn, source, ad_id, car, min_n=min_n), min_n, min_discount)
        new, flags = verdict(price, lo, mid, {"damage_type": damage, "reg_country": country},
                             title, description, bool(checked), similar, min_n)
        if state == "not cheap" and new in ("deal", "maybe", "unchecked"):
            new = "not cheap"
        watched = None
        if watchlist.is_set(wanted):
            watched = int(watchlist.matches(
                wanted, make, model, price, year, km,
                watchlist.discount(price, mid, similar[0], similar[2], min_n)))
        changed += new != old
        conn.execute(
            """UPDATE new_deals SET verdict = ?, flags = ?, similar_n = ?, similar_cheaper = ?,
                      similar_median = ?, watch = ? WHERE source = ? AND id = ?""",
            (new, "|".join(flags), *similar, watched, source, ad_id))
    conn.commit()
    return changed


# --------------------------------------------------------------------------- #


# verify: are the last N hours really all in the database?


# --------------------------------------------------------------------------- #


def verify(site, session, watch, hours, delay):
    """Walk the newest-first list back `hours` (by the cards' age badges) and
    list every ad that is not in the database. Read-only."""
    limit, checked, missing = hours * 60, 0, []
    counted = set()      # the newest ads are repeated on later pages: count each once
    blocks = BlockDetector(threshold=3)
    for page in range(1, 500):
        html = fetch(session, site.page_url(site.newest_url, page), blocks=blocks)
        rows = list(site.scrape_page(html)) if html else []
        if not rows:
            print("Could not read page", page, "- result is partial.")
            break
        ages = site.card_ages(html)
        for row in rows:
            age = ages.get(row["id"])
            if row["id"] in counted or (age is not None and age > limit):
                continue
            counted.add(row["id"])
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


def model_mtime():
    try:
        return (DATA_DIR / "price_model" / "meta.json").stat().st_mtime
    except OSError:
        return None

