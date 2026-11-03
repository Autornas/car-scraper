"""
collectors/dupes.py — the same car posted on more than one site.

Many sellers, dealers especially, post a car on Autoplius and autogidas at
once. Counted twice it would weigh double in the price model and show up as
two "deals". Matching is by the car itself: make, model and year must agree,
mileage within 2%, and nothing may contradict (fuel, gearbox, engine, kW).
On top of that a score:

    mileage   identical, not a round number      +3
              identical round number (230,000)   +2
              within 2%                          +1
    town      same                               +2     different  -3
    kW        same                               +1
    engine    same                               +0.5
    price     within 5%                          +2     within 15%  +1
              more than 35% apart                -3
    dealer    both listed by a dealer            +0.5

A pair needs MIN_SCORE, or MIN_SCORE_NEAR_KM when the mileage isn't identical
(a seller updating the odometer is real, but so are two similar cars). Pairs
are then taken best-first so every listing joins at most one other: one car,
one match per site.

Matches are kept in the `duplicates` table (rebuilt on every report build).
Of each pair, the cheaper listing is the one shown and priced; the other is
kept, linked from it, and left out of the price model's training.
"""

from collections import defaultdict
from datetime import datetime, timezone

MIN_SCORE = 4.0
MIN_SCORE_NEAR_KM = 5.0
MAX_KM_GAP = 0.02

SCHEMA = """
CREATE TABLE IF NOT EXISTS duplicates (
    source      TEXT NOT NULL,     -- the listing that is shown
    id          TEXT NOT NULL,
    dup_source  TEXT NOT NULL,     -- the same car on another site
    dup_id      TEXT NOT NULL,
    score       REAL,
    matched_at  TEXT,
    PRIMARY KEY (dup_source, dup_id)
);
CREATE INDEX IF NOT EXISTS idx_duplicates_shown ON duplicates(source, id);
"""


def _conflict(a, b, key):
    return bool(a.get(key)) and bool(b.get(key)) and a[key] != b[key]


def match_score(a, b):
    """How sure we are that listings a and b are the same car; 0 if impossible.
    Both are dicts with make, model, year, mileage_km, price_eur, fuel,
    engine_l, power_kw, gearbox, location, dealer."""
    if (a["make"], a["model"], a["year"]) != (b["make"], b["model"], b["year"]):
        return 0.0
    if any(_conflict(a, b, k) for k in ("fuel", "gearbox", "engine_l")):
        return 0.0
    if a.get("power_kw") and b.get("power_kw") and abs(a["power_kw"] - b["power_kw"]) > 2:
        return 0.0
    ka, kb = a.get("mileage_km"), b.get("mileage_km")
    if not ka or not kb or abs(ka - kb) > MAX_KM_GAP * max(ka, kb):
        return 0.0

    score = (3.0 if ka % 1000 else 2.0) if ka == kb else 1.0
    if a.get("location") and b.get("location"):
        score += 2.0 if a["location"].lower() == b["location"].lower() else -3.0
    if a.get("power_kw") and b.get("power_kw") and a["power_kw"] == b["power_kw"]:
        score += 1.0
    if a.get("engine_l") and a.get("engine_l") == b.get("engine_l"):
        score += 0.5
    pa, pb = a.get("price_eur"), b.get("price_eur")
    if pa and pb:
        gap = abs(pa - pb) / max(pa, pb)
        score += 2.0 if gap <= 0.05 else 1.0 if gap <= 0.15 else -3.0 if gap > 0.35 else 0.0
    if a.get("dealer") and b.get("dealer"):
        score += 0.5
    return score


def find_duplicates(cars, min_score=MIN_SCORE):
    """
    Pairs of listings from different sites that are the same car, as
    [(shown, other, score)]: `shown` is the cheaper of the two (the one a
    buyer would want), ties going to the canonical site. Each listing appears
    in at most one pair.
    """
    by_key = defaultdict(list)
    for c in cars:
        if c.get("mileage_km") and c.get("year"):
            by_key[(c["make"], c["model"], c["year"])].append(c)
    candidates = []
    for group in by_key.values():
        if len({c["source"] for c in group}) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if a["source"] != b["source"]:
                    s = match_score(a, b)
                    need = min_score if a["mileage_km"] == b["mileage_km"] \
                        else max(min_score, MIN_SCORE_NEAR_KM)
                    if s >= need:
                        candidates.append((s, a, b))
    candidates.sort(key=lambda t: -t[0])
    taken, pairs = set(), []
    for s, a, b in candidates:
        ka, kb = (a["source"], a["id"]), (b["source"], b["id"])
        if ka in taken or kb in taken:
            continue
        taken.update((ka, kb))
        pairs.append((*_order(a, b), s))
    return pairs


def _order(a, b):
    """(shown, other): the cheaper listing first; on a tie, Autoplius first."""
    pa, pb = a.get("price_eur") or 10**9, b.get("price_eur") or 10**9
    if pb < pa or (pb == pa and b["source"] == "autoplius" and a["source"] != "autoplius"):
        return b, a
    return a, b


def same_car_elsewhere(conn, source, car, min_score=MIN_SCORE):
    """
    The best match for one listing among the OTHER sites' listings, or None.
    Used by the watcher, so a new deal can say "also on autoplius €8,900".
    `car` needs the canonical make/model plus the fields match_score reads.
    """
    if not car.get("mileage_km") or not car.get("year"):
        return None
    cols = ("source", "id", "make", "model", "year", "mileage_km", "price_eur", "fuel",
            "engine_l", "power_kw", "gearbox", "location", "dealer", "link")
    rows = conn.execute(
        f"""SELECT {', '.join(cols)} FROM listings
            WHERE source != ? AND make = ? AND model = ? AND year = ?""",
        (source, car["make"], car["model"], car["year"])).fetchall()
    best = None
    for row in rows:
        other = dict(zip(cols, row))
        s = match_score(car, other)
        need = min_score if other["mileage_km"] == car["mileage_km"] \
            else max(min_score, MIN_SCORE_NEAR_KM)
        if s >= need and (best is None or s > best[0]):
            best = (s, other)
    return best[1] if best else None


def save_duplicates(conn, pairs):
    """Replace the duplicates table with these pairs."""
    conn.executescript(SCHEMA)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("DELETE FROM duplicates")
    conn.executemany(
        "INSERT OR REPLACE INTO duplicates VALUES (?,?,?,?,?,?)",
        [(a["source"], a["id"], b["source"], b["id"], round(s, 1), now) for a, b, s in pairs])
    conn.commit()
