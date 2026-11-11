"""
collectors/watchlist.py — which new deals you want to hear about.

One watchlist, edited on the dashboard's Watchlist page and kept in the
database (meta table, key "watchlist"), so it is backed up with everything
else:

    makes          ["bmw", "audi/a4"]   makes, or make/model pairs; [] = every make
    price_min/max  EUR; 0 = no limit
    year_min       0 = any year
    km_max         0 = any mileage (a car with no mileage shown still passes)
    min_discount   percent under the typical price: the median of similar cars,
                   or the model's expected price when there are too few of them
    include_maybe  also alert for "maybe" cars (too few similar cars to confirm)

What it changes:

  * every new deal that fits gets watch = 1 in new_deals: a star in the
    dashboard's New deals list and a "Watchlist only" filter;
  * Telegram alerts go out only for deals that fit. With no watchlist set,
    every confirmed deal is alerted, as before.
"""

import json
import re
import sqlite3

from collectors.common import name_slug
from collectors.store import CANONICAL_SOURCE

KEY = "watchlist"
EMPTY = {"makes": [], "price_min": 0, "price_max": 0, "year_min": 0, "km_max": 0,
         "min_discount": 0, "include_maybe": False}
NUMBERS = {"price_min": (0, 10_000_000), "price_max": (0, 10_000_000),
           "year_min": (0, 2100), "km_max": (0, 5_000_000), "min_discount": (0, 90)}
LABELS = {"price_min": "Price from", "price_max": "Price to", "year_min": "Year from",
          "km_max": "Mileage", "min_discount": "Discount"}


def is_set(watchlist):
    """True once any filter narrows anything."""
    return bool(watchlist["makes"]) or any(watchlist[name] for name in NUMBERS)


def load(conn):
    """The saved watchlist, or the empty one."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (KEY,)).fetchone()
    except sqlite3.Error:
        row = None
    watchlist = dict(EMPTY)
    if row and row[0]:
        try:
            saved = json.loads(row[0])
        except ValueError:
            saved = {}
        watchlist.update({k: v for k, v in saved.items() if k in EMPTY})
    return watchlist


def save(conn, watchlist):
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (KEY, json.dumps(watchlist)))
    conn.commit()


def known_names(conn):
    """(makes, make/model pairs) the database knows, to check what was typed."""
    try:
        pairs = set(conn.execute(
            """SELECT make, model FROM models WHERE source = ? AND model != '*'
               UNION SELECT DISTINCT make, model FROM listings""", (CANONICAL_SOURCE,)))
    except sqlite3.Error:
        pairs = set()
    return {make for make, _ in pairs if make}, pairs


def parse_makes(text, makes=(), pairs=()):
    """"BMW, audi a4, volkswagen/passat" -> ["bmw", "audi/a4", "volkswagen/passat"].
    Names are checked against the catalogue once there is one, so a typo is
    refused instead of silently matching nothing."""
    wanted = []
    for raw in re.split(r"[,;\n]+", text or ""):
        entry = raw.strip()
        if not entry:
            continue
        if "/" in entry:
            make, _, model = entry.partition("/")
            make, model = name_slug(make), name_slug(model)
        else:
            make, model = name_slug(entry), ""
            if makes and make not in makes:
                # "audi a4" -> audi / a4: the longest known make it starts with
                for known in sorted(makes, key=len, reverse=True):
                    if make.startswith(known + "-"):
                        make, model = known, make[len(known) + 1:]
                        break
        if not make:
            continue
        if makes and make not in makes:
            raise ValueError(f"Unknown make '{entry}'. Use the site's names, e.g. bmw, "
                             "mercedes-benz, volkswagen.")
        if model and pairs and (make, model) not in pairs:
            raise ValueError(f"Unknown model '{entry}'. Write it like the Models page "
                             "does, e.g. bmw/320 or audi/a4.")
        name = f"{make}/{model}" if model else make
        if name not in wanted:
            wanted.append(name)
    return wanted


def parse(payload, makes=(), pairs=()):
    """A watchlist from what the dashboard sent; ValueError says what is wrong."""
    watchlist = dict(EMPTY)
    text = payload.get("makes", "")
    if isinstance(text, list):
        text = ", ".join(str(item) for item in text)
    if not isinstance(text, str) or len(text) > 5000:
        raise ValueError("Makes must be a short list of names.")
    watchlist["makes"] = parse_makes(text, makes, pairs)
    for name, (low, high) in NUMBERS.items():
        value = payload.get(name) or 0
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not low <= value <= high:
            raise ValueError(f"{LABELS[name]} must be a number from {low} to {high:,}.")
        watchlist[name] = int(value)
    if watchlist["price_max"] and watchlist["price_min"] > watchlist["price_max"]:
        raise ValueError("Price from is higher than price to.")
    watchlist["include_maybe"] = bool(payload.get("include_maybe"))
    return watchlist


def discount(price, mid, similar_n=None, similar_median=None, min_similar=5):
    """How far under its typical price a car is (0.25 = 25% under): against
    the median of similar cars when there are enough, else the model's
    expected price. None when neither is known."""
    reference = similar_median if similar_median and (similar_n or 0) >= min_similar else mid
    return 1 - price / reference if reference and price else None


def matches(watchlist, make, model, price, year=None, km=None, discount_=None):
    """Does this car fit the watchlist? An empty watchlist fits nothing: there
    is nothing to mark."""
    if not is_set(watchlist) or not price:
        return False
    if watchlist["makes"] and make not in watchlist["makes"] \
            and f"{make}/{model}" not in watchlist["makes"]:
        return False
    if price < watchlist["price_min"] or (watchlist["price_max"]
                                          and price > watchlist["price_max"]):
        return False
    if watchlist["year_min"] and (not year or year < watchlist["year_min"]):
        return False
    if watchlist["km_max"] and km and km > watchlist["km_max"]:
        return False
    if watchlist["min_discount"] and (discount_ is None
                                      or discount_ * 100 < watchlist["min_discount"]):
        return False
    return True


def should_alert(watchlist, verdict, watched):
    """Whether a new car is worth a Telegram message. Without a watchlist:
    every confirmed deal. With one: only cars that fit it, and "maybe" cars
    too if asked for."""
    if not is_set(watchlist):
        return verdict == "deal"
    wanted = ("deal", "maybe") if watchlist["include_maybe"] else ("deal",)
    return verdict in wanted and bool(watched)


def reflag(conn, watchlist, min_similar=5):
    """Mark the deals already listed against a (new) watchlist. Returns how
    many fit."""
    rows = conn.execute(
        """SELECT source, id, make, model, price, year, mileage_km, mid,
                  similar_n, similar_median FROM new_deals""").fetchall()
    fits = 0
    for source, ad_id, make, model, price, year, km, mid, similar_n, similar_median in rows:
        watched = matches(watchlist, make, model, price, year, km,
                          discount(price, mid, similar_n, similar_median, min_similar))
        fits += watched
        conn.execute("UPDATE new_deals SET watch = ? WHERE source = ? AND id = ?",
                     (int(watched) if is_set(watchlist) else None, source, ad_id))
    conn.commit()
    return fits


def describe(watchlist):
    """One line for the audit log and messages."""
    if not is_set(watchlist):
        return "no watchlist: every confirmed deal"
    parts = [", ".join(watchlist["makes"]) or "every make"]
    if watchlist["price_min"] or watchlist["price_max"]:
        parts.append(f"€{watchlist['price_min']:,}–"
                     + (f"{watchlist['price_max']:,}" if watchlist["price_max"] else "any"))
    if watchlist["year_min"]:
        parts.append(f"from {watchlist['year_min']}")
    if watchlist["km_max"]:
        parts.append(f"up to {watchlist['km_max']:,} km")
    if watchlist["min_discount"]:
        parts.append(f"at least {watchlist['min_discount']}% under")
    if watchlist["include_maybe"]:
        parts.append("maybe cars too")
    return "; ".join(parts)
