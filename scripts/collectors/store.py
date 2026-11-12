"""
collectors/store.py — the listings database, shared by every site.

    store = Store("data/autoplius.db")          # opens, creates or migrates it
    db = store.site("autoplius")                # everything for one site
    db.upsert_many(rows, "bmw", "320")
    store.close()

Every listing, price change and model belongs to a `source` (the site), and a
listing's key is (source, id): ad ids from two sites can collide.

Make / model names. Listings are stored under the CANONICAL make/model (the
names the price model groups by). A site whose names differ maps them in
model_aliases (source, site_make, site_model -> make, model); with no alias a
name is used as it is, which is how Autoplius' names became the canonical ones.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2
# The site whose make/model names are the shared ones (see model_aliases).
CANONICAL_SOURCE = "autoplius"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    source       TEXT NOT NULL,
    id           TEXT NOT NULL,
    make         TEXT,
    model        TEXT,
    title        TEXT,
    year         INTEGER,
    price_eur    INTEGER,
    mileage_km   INTEGER,
    fuel         TEXT,
    engine_l     TEXT,
    link         TEXT,
    first_seen   TEXT,
    last_seen    TEXT,
    seen_count   INTEGER DEFAULT 1,
    description  TEXT,
    description_checked_at TEXT,
    power_kw     INTEGER,
    gearbox      TEXT,
    body         TEXT,
    location     TEXT,
    dealer       INTEGER,
    damaged      INTEGER,     -- 1 = in the site's damaged-only search, 0 = not, NULL = unknown
    damage_type  TEXT,        -- the ad page's own "Defektai" value
    reg_country  TEXT,        -- "Pirmosios registracijos šalis"
    removed_at   TEXT,        -- when it was found taken down: its ad page answered
                              -- "not found", or a full scrape of its model missed it
    PRIMARY KEY (source, id)
);
CREATE INDEX IF NOT EXISTS idx_make_model ON listings(make, model);
CREATE INDEX IF NOT EXISTS idx_year       ON listings(year);
CREATE INDEX IF NOT EXISTS idx_last_seen  ON listings(last_seen);
CREATE INDEX IF NOT EXISTS idx_description_missing ON listings(description_checked_at);

CREATE TABLE IF NOT EXISTS price_history (
    source    TEXT NOT NULL,
    id        TEXT NOT NULL,
    seen_at   TEXT,
    price_eur INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ph_source_id ON price_history(source, id);

CREATE TABLE IF NOT EXISTS models (
    url          TEXT PRIMARY KEY,
    source       TEXT NOT NULL DEFAULT 'autoplius',
    make         TEXT,          -- the site's own names
    model        TEXT,
    last_scraped TEXT,
    found        INTEGER,
    was_capped   INTEGER DEFAULT 0,
    make_id      INTEGER,
    model_id     INTEGER,
    make_name    TEXT,
    model_name   TEXT,
    enabled      INTEGER DEFAULT 1   -- 0 = switched off; a catalogue sync never undoes it
);
CREATE INDEX IF NOT EXISTS idx_models_source ON models(source, make, model);

CREATE TABLE IF NOT EXISTS model_aliases (
    source     TEXT NOT NULL,
    site_make  TEXT NOT NULL,
    site_model TEXT NOT NULL,
    make       TEXT NOT NULL,       -- canonical
    model      TEXT NOT NULL,
    PRIMARY KEY (source, site_make, site_model)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns the listings table gained over time, for databases older than v2.
V1_EXTRA_COLUMNS = {
    "description": "TEXT", "description_checked_at": "TEXT", "power_kw": "INTEGER",
    "gearbox": "TEXT", "body": "TEXT", "location": "TEXT", "dealer": "INTEGER",
    "damaged": "INTEGER", "damage_type": "TEXT", "reg_country": "TEXT",
}
V1_MODEL_COLUMNS = {
    "make_id": "INTEGER", "model_id": "INTEGER", "make_name": "TEXT",
    "model_name": "TEXT", "enabled": "INTEGER DEFAULT 1",
}
LISTING_COLUMNS = ["id", "make", "model", "title", "year", "price_eur", "mileage_km",
                   "fuel", "engine_l", "link", "first_seen", "last_seen", "seen_count",
                   *V1_EXTRA_COLUMNS]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def migrate_v1_to_v2(conn, path):
    """
    Single-site database (listings keyed by id alone) -> multi-site. Every
    existing row becomes source 'autoplius', the only site there was. A copy
    of the file is saved first, next to it, as <name>.pre-v2-<timestamp>.
    """
    backup = Path(f"{path}.pre-v2-{datetime.now():%Y%m%d-%H%M%S}")
    with sqlite3.connect(backup) as dst:
        conn.backup(dst)
    print(f"Upgrading the database to the multi-site layout (backup: {backup.name})")

    have = _columns(conn, "listings")
    for name, kind in V1_EXTRA_COLUMNS.items():          # very old files first
        if name not in have:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {name} {kind}")
    if "models" in _tables(conn):
        have = _columns(conn, "models")
        for name, kind in V1_MODEL_COLUMNS.items():
            if name not in have:
                conn.execute(f"ALTER TABLE models ADD COLUMN {name} {kind}")
    conn.commit()

    cols = ", ".join(LISTING_COLUMNS)
    conn.executescript(f"""
        BEGIN;
        ALTER TABLE listings RENAME TO listings_v1;
        DROP INDEX IF EXISTS idx_make_model;
        DROP INDEX IF EXISTS idx_year;
        DROP INDEX IF EXISTS idx_last_seen;
        DROP INDEX IF EXISTS idx_description_missing;
        {SCHEMA.split('CREATE TABLE IF NOT EXISTS price_history')[0]}
        INSERT INTO listings (source, {cols}) SELECT 'autoplius', {cols} FROM listings_v1;
        DROP TABLE listings_v1;

        ALTER TABLE price_history RENAME TO price_history_v1;
        DROP INDEX IF EXISTS idx_ph_id;
        CREATE TABLE price_history (source TEXT NOT NULL, id TEXT NOT NULL,
                                    seen_at TEXT, price_eur INTEGER);
        INSERT INTO price_history SELECT 'autoplius', id, seen_at, price_eur
            FROM price_history_v1;
        DROP TABLE price_history_v1;
        COMMIT;
    """)
    if "models" in _tables(conn) and "source" not in _columns(conn, "models"):
        conn.execute("ALTER TABLE models ADD COLUMN source TEXT NOT NULL DEFAULT 'autoplius'")
    if "meta" in _tables(conn):
        for key in ("catalogue_synced_at", "damage_sweep_at"):
            conn.execute("UPDATE meta SET key = ? WHERE key = ?", (f"{key}:autoplius", key))
    conn.commit()


class Store:
    """The database file. One connection, one lock; per-site work goes through
    `store.site(source)`."""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The watcher, the dashboard and a manual run can all have the file open:
        # WAL lets readers work during a write, and the timeout makes a writer
        # wait its turn instead of failing with "database is locked".
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        if "listings" in _tables(self.conn) and "source" not in _columns(self.conn, "listings"):
            migrate_v1_to_v2(self.conn, path)
        if "listings" in _tables(self.conn) and "removed_at" not in _columns(self.conn, "listings"):
            self.conn.execute("ALTER TABLE listings ADD COLUMN removed_at TEXT")
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
                          (str(SCHEMA_VERSION),))
        self.conn.commit()
        self.lock = threading.Lock()
        self.inserted = 0
        self.updated = 0
        self.price_changes = 0

    def site(self, source):
        return SiteStore(self, source)

    def meta(self, key):
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def counts(self):
        with self.lock:
            total = self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
            models_total, models_done = self.conn.execute(
                """SELECT COUNT(*), COUNT(last_scraped) FROM models
                   WHERE enabled = 1""").fetchone()
        return total, models_done, models_total

    def close(self):
        with self.lock:
            self.conn.close()


class SiteStore:
    """Everything the database does for one site (`source`)."""

    def __init__(self, store, source):
        self.store, self.source = store, source
        self.conn, self.lock = store.conn, store.lock
        self._aliases = None

    # ------------------------------------------------------------ names
    def canonical(self, make, model):
        """The canonical make/model for this site's names (see model_aliases)."""
        with self.lock:
            return self.canonical_unlocked(make, model)

    def set_alias(self, site_make, site_model, make, model):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO model_aliases VALUES (?,?,?,?,?)",
                              (self.source, site_make, site_model, make, model))
            self.conn.commit()
        self._aliases = None

    # ------------------------------------------------------------ listings
    def upsert_many(self, rows, make, model):
        """Insert new listings, refresh known ones. `make` / `model` (or each
        row's _make / _model) are the site's names; they're stored canonical."""
        if not rows:
            return
        ts = now_iso()
        store = self.store
        with self.lock:
            cur = self.conn.cursor()
            for r in rows:
                price = r["price_eur"] if r["price_eur"] != "" else None
                row_make, row_model = self.canonical_unlocked(
                    r.get("_make", make), r.get("_model", model))
                existing = cur.execute(
                    "SELECT price_eur FROM listings WHERE source = ? AND id = ?",
                    (self.source, r["id"])).fetchone()
                card = (r.get("power_kw"), r.get("gearbox"), r.get("body"),
                        r.get("location"), r.get("dealer"))
                if existing is None:
                    cur.execute(
                        """INSERT INTO listings
                           (source, id, make, model, title, year, price_eur, mileage_km,
                            fuel, engine_l, link, first_seen, last_seen, seen_count,
                            power_kw, gearbox, body, location, dealer)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)""",
                        (self.source, r["id"], row_make, row_model, r["title"],
                         r["year"] or None, price,
                         r["mileage_km"] if r["mileage_km"] != "" else None,
                         r["fuel"], r["engine_l"], r["link"], ts, ts, *card))
                    if price is not None:
                        cur.execute("INSERT INTO price_history VALUES (?,?,?,?)",
                                    (self.source, r["id"], ts, price))
                    store.inserted += 1
                else:
                    # Write the price we actually parsed, even when it's NULL.
                    # COALESCE here would preserve a previously mis-parsed price
                    # (e.g. a lease payment) forever, so a re-scrape could never
                    # repair it. price_history keeps the old value regardless.
                    # Card details only ever fill in: a source that doesn't parse
                    # them must not blank what a full scrape found.
                    cur.execute(
                        """UPDATE listings
                           SET last_seen = ?, seen_count = seen_count + 1,
                               removed_at = NULL,
                               price_eur = ?, make = ?, model = ?,
                               mileage_km = COALESCE(?, mileage_km),
                               power_kw = COALESCE(?, power_kw),
                               gearbox  = COALESCE(?, gearbox),
                               body     = COALESCE(?, body),
                               location = COALESCE(?, location),
                               dealer   = COALESCE(?, dealer)
                           WHERE source = ? AND id = ?""",
                        # Mileage is re-read too, so a scrape repairs a
                        # mis-parsed one (the "3,000 km" warranty badge).
                        (ts, price, row_make, row_model,
                         r["mileage_km"] if r["mileage_km"] != "" else None,
                         *card, self.source, r["id"]))
                    if price is not None and price != existing[0]:
                        cur.execute("INSERT INTO price_history VALUES (?,?,?,?)",
                                    (self.source, r["id"], ts, price))
                        store.price_changes += 1
                    store.updated += 1
            self.conn.commit()

    def canonical_unlocked(self, make, model):
        """canonical() for callers already holding the lock."""
        if self._aliases is None:
            self._aliases = {
                (mk, md): (cmk, cmd) for mk, md, cmk, cmd in self.conn.execute(
                    """SELECT site_make, site_model, make, model FROM model_aliases
                       WHERE source = ?""", (self.source,))}
        return self._aliases.get((make, model), (make, model))

    def is_known(self, ad_id):
        with self.lock:
            return self.conn.execute(
                "SELECT 1 FROM listings WHERE source = ? AND id = ?",
                (self.source, ad_id)).fetchone() is not None

    def missing_descriptions(self, limit=None, ids=None):
        """Unchecked listings as (id, link), oldest first — or, given `ids`, only
        those, in the order given (the stats candidate file is best-deal first)."""
        with self.lock:
            if ids is not None:
                rows = []
                for ad_id in ids:
                    row = self.conn.execute(
                        """SELECT id, link FROM listings WHERE source = ? AND id = ?
                           AND description_checked_at IS NULL""",
                        (self.source, ad_id)).fetchone()
                    if row:
                        rows.append(row)
                return rows[:limit] if limit is not None else rows
            query = """SELECT id, link FROM listings
                       WHERE source = ? AND description_checked_at IS NULL
                       ORDER BY first_seen, id"""
            params = (self.source,) if limit is None else (self.source, limit)
            return self.conn.execute(query + ("" if limit is None else " LIMIT ?"),
                                     params).fetchall()

    def descriptions_remaining(self):
        with self.lock:
            return self.conn.execute(
                """SELECT COUNT(*) FROM listings
                   WHERE source = ? AND description_checked_at IS NULL""",
                (self.source,)).fetchone()[0]

    def save_description(self, ad_id, description, details=None):
        """Save the description and whatever the ad's parameter table gave.
        A declared defect on the ad page also sets damaged = 1."""
        details = details or {}
        damage = details.get("damage_type")
        with self.lock:
            self.conn.execute(
                """UPDATE listings
                   SET description = ?, description_checked_at = ?,
                       power_kw    = COALESCE(?, power_kw),
                       gearbox     = COALESCE(?, gearbox),
                       body        = COALESCE(?, body),
                       damage_type = ?,
                       damaged     = CASE WHEN ? IS NOT NULL THEN 1 ELSE damaged END,
                       reg_country = COALESCE(?, reg_country)
                   WHERE source = ? AND id = ?""",
                (description, now_iso(), details.get("power_kw"),
                 details.get("gearbox"), details.get("body"), damage, damage,
                 details.get("reg_country"), self.source, ad_id))
            self.conn.commit()

    def mark_removed(self, ad_id):
        """Its ad page answered "not found": the car was taken down. It also
        counts as read, so the nightly reads don't try it again."""
        with self.lock:
            self.conn.execute(
                """UPDATE listings SET removed_at = ?, description_checked_at = ?
                   WHERE source = ? AND id = ?""",
                (now_iso(), now_iso(), self.source, ad_id))
            self.conn.commit()

    def stamp_gone(self, ids):
        """Record when each of these listings was found gone (a full scrape of
        its model no longer lists it). Its last price stays: that is what the
        car was really asking when it went. Already stamped ones keep their
        date; a listing seen again later is un-stamped by upsert_many.
        Returns how many were newly stamped."""
        if not ids:
            return 0
        with self.lock:
            stamp = now_iso()
            n = 0
            for ad_id in ids:
                n += self.conn.execute(
                    """UPDATE listings SET removed_at = ?
                       WHERE source = ? AND id = ? AND removed_at IS NULL""",
                    (stamp, self.source, ad_id)).rowcount
            self.conn.commit()
        return n

    def apply_damage_sweep(self, damaged_ids):
        """Record one complete site-wide damaged search: those ids are damaged,
        every other listing of this site is not, unless its own ad page
        declared a defect. Returns how many of this site's listings are damaged."""
        with self.lock:
            self.conn.execute(
                "UPDATE listings SET damaged = 0 WHERE source = ? AND damage_type IS NULL",
                (self.source,))
            self.conn.executemany(
                "UPDATE listings SET damaged = 1 WHERE source = ? AND id = ?",
                [(self.source, i) for i in damaged_ids])
            self._set_meta("damage_sweep_at")
            self.conn.commit()
            return self.conn.execute(
                "SELECT COUNT(*) FROM listings WHERE source = ? AND damaged = 1",
                (self.source,)).fetchone()[0]

    # ------------------------------------------------------------ models
    def mark_model(self, url, make, model, found, capped):
        with self.lock:
            self.conn.execute(
                """INSERT INTO models (url, source, make, model, last_scraped, found, was_capped)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET
                     last_scraped = excluded.last_scraped,
                     found = excluded.found,
                     was_capped = excluded.was_capped""",
                (url, self.source, make, model, now_iso(), found, 1 if capped else 0))
            self.conn.commit()

    def sync_catalogue(self, entries, url_for):
        """
        Merge the site's catalogue into `models`. A model already synced is
        found by its id; a model from before the catalogue (same make/model
        name, no id yet) keeps its progress and listings and switches to the
        id-based URL; anything else is added, enabled and unscraped. A few
        names carry two ids on the site (VW "LT"); both rows share the name.
        `url_for(entry)` builds the site's search URL. Returns (updated, added).
        """
        updated = added = 0
        with self.lock:
            for e in entries:
                url = url_for(e)
                row = None
                if e["model_id"] is not None:
                    row = self.conn.execute(
                        "SELECT rowid FROM models WHERE source = ? AND model_id = ?",
                        (self.source, e["model_id"])).fetchone()
                if row is None and e["model_id"] is None:
                    # a site without ids (autogidas): the name is the identity
                    row = self.conn.execute(
                        "SELECT rowid FROM models WHERE source = ? AND make = ? AND model = ?",
                        (self.source, e["make"], e["model"])).fetchone()
                elif row is None:
                    row = self.conn.execute(
                        """SELECT rowid FROM models WHERE source = ? AND make = ?
                           AND model = ? AND model_id IS NULL""",
                        (self.source, e["make"], e["model"])).fetchone()
                if row:
                    self.conn.execute(
                        """UPDATE models SET url = ?, make_id = ?, model_id = ?,
                               make_name = ?, model_name = ?
                           WHERE rowid = ?""",
                        (url, e["make_id"], e["model_id"], e["make_name"],
                         e["model_name"], row[0]))
                    updated += 1
                else:
                    self.conn.execute(
                        """INSERT OR IGNORE INTO models (url, source, make, model, make_id,
                               model_id, make_name, model_name, enabled)
                           VALUES (?,?,?,?,?,?,?,?,1)""",
                        (url, self.source, e["make"], e["model"], e["make_id"],
                         e["model_id"], e["make_name"], e["model_name"]))
                    added += 1
            self._set_meta("catalogue_synced_at")
            self.conn.commit()
        return updated, added

    def catalogue_age_days(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT julianday('now') - julianday(value) FROM meta WHERE key = ?",
                (f"catalogue_synced_at:{self.source}",)).fetchone()
        return row[0] if row else None

    def models_missing_ids(self):
        with self.lock:
            return self.conn.execute(
                """SELECT COUNT(*) FROM models WHERE source = ? AND model_id IS NULL
                   AND model != '*'""",
                (self.source,)).fetchone()[0]

    def pending_models(self, max_age_days, force, empty_max_age_days=30):
        """Enabled models due for a scrape, as (url, make, model), never-scraped
        first, then oldest first, so a limited run takes the stalest. A model whose
        last scrape found no ads (old Mercedes 290 GD and the like) is rechecked
        only every `empty_max_age_days`, so thousands of empty models don't
        cost a request each every week."""
        with self.lock:
            if force:
                return self.conn.execute(
                    """SELECT url, make, model FROM models
                       WHERE source = ? AND enabled = 1 ORDER BY make, model""",
                    (self.source,)).fetchall()
            return self.conn.execute(
                """SELECT url, make, model FROM models
                   WHERE source = ? AND enabled = 1 AND (
                       last_scraped IS NULL
                       OR (COALESCE(found, 1) > 0
                           AND julianday('now') - julianday(last_scraped) > ?)
                       OR (found = 0
                           AND julianday('now') - julianday(last_scraped) > ?))
                   ORDER BY last_scraped IS NOT NULL, last_scraped, make, model""",
                (self.source, max_age_days, empty_max_age_days)).fetchall()

    def models_for_make(self, make):
        with self.lock:
            return [row[0] for row in self.conn.execute(
                "SELECT model FROM models WHERE source = ? AND make = ? ORDER BY model",
                (self.source, make))]

    def canonical_pairs(self):
        """Every canonical make/model: the canonical site's catalogue plus every
        make/model any listing is stored under. Card titles from any site are
        matched against these, so a car lands under the same name everywhere."""
        with self.lock:
            return self.conn.execute(
                """SELECT make, model FROM models WHERE source = ? AND model != '*'
                   UNION SELECT DISTINCT make, model FROM listings""",
                (CANONICAL_SOURCE,)).fetchall()

    def _set_meta(self, key):
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                          (f"{key}:{self.source}", now_iso()))


# ---------------------------------------------------------------- maintenance
def find_gone(store, source=None):
    """
    (source, id) of every listing that is GONE: its model's last full scrape
    finished more than an hour after the listing was last seen, i.e. that
    scrape walked the whole model and didn't find it. Same definition as
    autoplius_stats.mark_disappeared(), so "gone" means the same everywhere.

    The reference is models.last_scraped, never the newest listing: the watcher
    inserts single new ads at any time and must not make the rest of a model
    look sold. The hour of slack covers a long model scrape, whose pages are
    stamped minutes before the model is marked done.
    """
    def ts(value):
        try:
            return datetime.fromisoformat(value) if value else None
        except ValueError:
            return None

    with store.lock:
        scraped = {}
        # models carry each site's own names, listings the canonical ones
        for src, mk, md, at in store.conn.execute(
                """SELECT m.source, COALESCE(a.make, m.make), COALESCE(a.model, m.model),
                          m.last_scraped
                   FROM models m LEFT JOIN model_aliases a
                     ON a.source = m.source AND a.site_make = m.make
                    AND a.site_model = m.model"""):
            key, t = (src, mk, md), ts(at)
            if t and (key not in scraped or t > scraped[key]):
                scraped[key] = t
        rows = store.conn.execute(
            "SELECT source, id, make, model, last_seen FROM listings"
            + (" WHERE source = ?" if source else ""),
            (source,) if source else ()).fetchall()
    gone = []
    for src, rid, make, model, last_seen in rows:
        # A site searched per make (autogidas) has one "*" model per make, while
        # its cars are stored under their real models: the make's scrape counts.
        seen = ts(last_seen)
        ref = scraped.get((src, make, model)) or scraped.get((src, make, "*"))
        if seen and ref and (ref - seen).total_seconds() > 3600:
            gone.append((src, rid))
    return gone


def do_export(store, path):
    import csv
    cols = ["source", "id", "make", "model", "title", "year", "price_eur", "mileage_km",
            "fuel", "engine_l", "power_kw", "gearbox", "body", "damaged", "link",
            "first_seen", "last_seen", "seen_count"]
    with store.lock:
        rows = store.conn.execute(f"SELECT {','.join(cols)} FROM listings").fetchall()
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerows(rows)
    print(f"Exported {len(rows)} listings to {path}")


def do_prune_gone(store, dry_run):
    gone = find_gone(store)
    total = store.counts()[0]
    if not gone:
        print("Nothing to prune — no listing is behind its model's most recent scrape.")
        return
    print(f"{len(gone)} of {total} listings are gone (not seen in their model's "
          f"most recent scrape).")
    if dry_run:
        print("Dry run — nothing deleted. Re-run without --dry-run to remove them.")
        return
    with store.lock:
        cur = store.conn.cursor()
        cur.executemany("DELETE FROM price_history WHERE source = ? AND id = ?", gone)
        cur.executemany("DELETE FROM listings WHERE source = ? AND id = ?", gone)
        store.conn.commit()
    print(f"Deleted {len(gone)} listings (and their price history). "
          f"{store.counts()[0]} listings remain.")


def do_status(store):
    total, done, models_total = store.counts()
    print(f"Listings stored : {total}")
    with store.lock:
        for source, n in store.conn.execute(
                "SELECT source, COUNT(*) FROM listings GROUP BY source"):
            print(f"  {source:<14}: {n}")
    print(f"Models scraped  : {done} / {models_total} enabled")
    with store.lock:
        top = store.conn.execute(
            """SELECT make, model, COUNT(*) c FROM listings
               GROUP BY make, model ORDER BY c DESC LIMIT 15""").fetchall()
        capped = store.conn.execute(
            "SELECT COUNT(*) FROM models WHERE was_capped = 1").fetchone()[0]
    if capped:
        print(f"Models that needed price-splitting: {capped}")
    if top:
        print("\nBiggest model groups:")
        for make, model, c in top:
            print(f"  {c:6d}  {make}/{model}")
