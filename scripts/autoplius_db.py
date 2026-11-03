#!/usr/bin/env python3
"""
autoplius_db.py — build the baseline dataset for price analysis.

What it does differently from the CSV scrapers
----------------------------------------------
* Walks the site's OWN model list (from their published sitemap) and scrapes
  one model at a time, via /naudoti-automobiliai/<make>/<model>. That means:
    - make and model come from the URL, authoritative, not guessed from a slug
    - each model is far below the ~200-page pagination cap, so nothing is lost
    - it's naturally chunked and resumable, model by model
* Stores into SQLite, not CSV, recording first_seen / last_seen / seen_count
  and a price history row every time a listing's price changes.
  That history is what later tells you which cars sold fast (= priced well)
  and which sat for months (= overpriced, and quietly poisoning your averages).
* If a model somehow still hits the cap, it splits that model by price band
  and recurses, so even Passat/Golf-sized models come back complete.

Setup
-----
    pip install requests beautifulsoup4

This file is SELF-CONTAINED — it needs no other script of mine next to it.

Run
---
    python scripts/autoplius_db.py --refresh-models  # fetch the make/model list once
    python scripts/autoplius_db.py --limit 50        # scrape 50 models, then stop
    python scripts/autoplius_db.py                   # scrape everything outstanding

    python scripts/autoplius_db.py --status
    python scripts/autoplius_db.py --prune-gone --dry-run
    python scripts/autoplius_db.py --prune-gone
    python scripts/autoplius_db.py --export data/cars.csv

Re-run any time — models already scraped are skipped unless they're stale
(--max-age days) or you pass --force.
"""

import argparse
import csv
import re
import signal
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

from settings import SETTINGS, project_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = project_path(SETTINGS["paths"]["data_dir"])
CFG = SETTINGS["autoplius"]

# =========================================================================== #
# Fetching + parsing engine
# =========================================================================== #
BASE = "https://autoplius.lt"
DEFAULT_SEARCH = f"{BASE}/skelbimai/naudoti-automobiliai"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8",
}

# /skelbimai/bmw-320-2-0-l-sedanas-2007-dyzelinas-31767371.html
AD_HREF_RE = re.compile(r"/skelbimai/[^/?#]+-(\d+)\.html")

FUELS = {
    "benzinas": "Benzinas",
    "dyzelinas": "Dyzelinas",
    "benzinas/elektra": "Benzinas/elektra",
    "dyzelinas/elektra": "Dyzelinas/elektra",
    "elektra": "Elektra",
    "dujos": "Dujos",
    "benzinas/dujos": "Benzinas/dujos",
    "hibridas": "Hibridas",
}
# longest first, so "benzinas/elektra" wins over "benzinas"
FUEL_KEYS = sorted(FUELS, key=len, reverse=True)

FIELDS = ["id", "title", "year", "price_eur", "mileage_km", "fuel", "engine_l", "link",
          "power_kw", "gearbox", "body", "location", "dealer"]

GEARBOXES = {"automatinė": "Automatinė", "mechaninė": "Mechaninė"}

# Every value of the site's own "Defektai" filter except 10924 ("Be defektų").
# Ticking several is an OR, so one search per model returns every car whose
# seller declared a defect: Daužtas, Degęs, Pavarų dėžės defektas, Pažeistas
# krušos, Skendęs, Variklio defektas, Kiti stambūs defektai.
DAMAGE_IDS = (10925, 18967, 18969, 19062, 18966, 18968, 18970)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def page_url(url: str, page_nr: int) -> str:
    """Return `url` with the autoplius page parameter set to `page_nr`."""
    parts = urlparse(url)
    query = parse_qs(parts.query)
    query["page_nr"] = [str(page_nr)]
    return urlunparse(parts._replace(query=urlencode(query, doseq=True)))


class BlockDetector:
    """
    Notices when the site is refusing us (403 / connection resets) and trips
    once it's clearly not going to serve us — so we stop grinding through
    2000 pages against a closed door. Also saves the first refused response
    so you can SEE what kind of refusal it is.
    """

    def __init__(self, threshold=8, dump_path=DATA_DIR / "blocked_response.html"):
        self.threshold = threshold
        self.dump_path = Path(dump_path)
        self.first_status = None
        self.tripped = threading.Event()
        self._consecutive = 0
        self._dumped = False
        self._lock = threading.Lock()

    def record_refusal(self, status=None, body=None):
        with self._lock:
            self._consecutive += 1
            if self.first_status is None and status is not None:
                self.first_status = status
            if body and not self._dumped:
                try:
                    self.dump_path.parent.mkdir(parents=True, exist_ok=True)
                    self.dump_path.write_text(body, encoding="utf-8", errors="replace")
                    self._dumped = True
                    print(f"  ! saved the refused page to {self.dump_path.name} — "
                          f"open it in a browser to see what the site actually sent",
                          file=sys.stderr)
                except OSError:
                    pass
            if self._consecutive >= self.threshold:
                self.tripped.set()

    def record_success(self):
        with self._lock:
            self._consecutive = 0


def diagnose(body: str) -> str:
    """Guess the kind of refusal from the page the server sent back."""
    low = (body or "").lower()
    if any(k in low for k in ("recaptcha", "hcaptcha", "turnstile", "g-recaptcha")):
        return "a real CAPTCHA widget — the site wants a human to solve something"
    if any(k in low for k in ("checking your browser", "cf-browser-verification",
                              "just a moment", "enable javascript and cookies")):
        return "a Cloudflare browser challenge (JavaScript check)"
    if "cloudflare" in low and ("access denied" in low or "error 1006" in low
                               or "error 1007" in low or "error 1008" in low):
        return "Cloudflare blocking this IP outright (no challenge offered)"
    if any(k in low for k in ("access denied", "forbidden", "prieiga uždrausta",
                              "blocked", "ip address has been")):
        return "a flat 'access denied' — this path is refused to this client"
    if not low.strip():
        return "an empty body — that's a firewall/IP-level drop, not a CAPTCHA"
    return "an unrecognised refusal page — open the saved file to see it"


def fetch(session: requests.Session, url: str, retries: int = 3, blocks=None) -> str:
    """GET a page, retrying a couple of times on transient errors."""
    for attempt in range(1, retries + 1):
        if blocks is not None and blocks.tripped.is_set():
            return ""
        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 200:
                resp.encoding = resp.apparent_encoding or "utf-8"
                if blocks is not None:
                    blocks.record_success()
                return resp.text
            print(f"  ! HTTP {resp.status_code} for {url}", file=sys.stderr)
            if resp.status_code in (401, 403, 429, 503):
                # a refusal won't clear by asking again 3 seconds later from the
                # same IP — record it and move on instead of adding load
                if blocks is not None:
                    blocks.record_refusal(resp.status_code, resp.text)
                return ""
        except requests.RequestException as exc:
            print(f"  ! {exc.__class__.__name__}: {exc}", file=sys.stderr)
            if blocks is not None:
                blocks.record_refusal()
        if attempt < retries:
            time.sleep(3 * attempt)
    return ""


def parse_description(html: str):
    """Extract the seller-written description, without inferring vehicle defects."""
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one(".section.comments-container")
    if container is None:
        container = soup.select_one(".comments-container")
    if container is None:
        return None

    for heading in container.select(
        ".announcement-content-title, .comment-translation-title"
    ):
        heading.decompose()
    text = container.get_text("\n", strip=True)
    text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text)
    return text or None


def parse_ad_details(html: str):
    """The ad page's parameter table, reduced to the fields the price model uses.

        Variklis                      | 1997 cm³, 184 AG (135kW)
        Pavarų dėžė                   | Automatinė
        Kėbulo tipas                  | Sedanas
        Defektai                      | Pažeistas krušos      (absent if none)
        Pirmosios registracijos šalis | JAV
    """
    soup = BeautifulSoup(html, "html.parser")
    table = {}
    for row in soup.select(".parameter-row"):
        label, value = row.select_one(".parameter-label"), row.select_one(".parameter-value")
        if label and value:
            table[label.get_text(" ", strip=True)] = value.get_text(" ", strip=True)
    if not table:
        return {}
    power = re.search(r"(\d{2,4})\s*kW", table.get("Variklis", ""))
    return {
        "power_kw": int(power.group(1)) if power else None,
        "gearbox": table.get("Pavarų dėžė") or None,
        "body": table.get("Kėbulo tipas") or None,
        "damage_type": table.get("Defektai") or None,
        "reg_country": table.get("Pirmosios registracijos šalis") or None,
    }


def fetch_description(session: requests.Session, url: str, blocks=None):
    """Fetch one public ad page; return (description, checked, HTTP status)."""
    description, _, completed, status = fetch_ad_details(session, url, blocks)
    return description, completed, status


def fetch_ad_details(session: requests.Session, url: str, blocks=None):
    """Fetch one public ad page; return (description, details, checked, HTTP status)."""
    parts = urlparse(url)
    if parts.scheme != "https" or parts.hostname not in {"autoplius.lt", "www.autoplius.lt"}:
        raise ValueError(f"Refusing a non-Autoplius listing URL: {url}")

    try:
        response = session.get(url, headers=HEADERS, timeout=30)
    except requests.RequestException as exc:
        print(f"  ! {exc.__class__.__name__}: {exc}", file=sys.stderr)
        if blocks is not None:
            blocks.record_refusal()
        return None, {}, False, None

    if response.status_code != 200:
        print(f"  ! HTTP {response.status_code} for {url}", file=sys.stderr)
        if blocks is not None:
            blocks.record_refusal(response.status_code, response.text)
        return None, {}, False, response.status_code

    response.encoding = response.apparent_encoding or "utf-8"
    body = response.text
    description = parse_description(body)
    challenge_markers = (
        "checking your browser", "cf-browser-verification", "just a moment",
        "enable javascript and cookies", "cf-turnstile", "g-recaptcha",
        "hcaptcha",
    )
    if description is None and any(marker in body.lower() for marker in challenge_markers):
        print(f"  ! verification challenge for {url}", file=sys.stderr)
        if blocks is not None:
            blocks.record_refusal(response.status_code, body)
        return None, {}, False, response.status_code

    if blocks is not None:
        blocks.record_success()
    return description, parse_ad_details(body), True, response.status_code


def clean_number(text: str):
    """'22 750' / '22.750' -> 22750"""
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def normalized_label(value: str) -> str:
    """Normalize make/model labels for matching listing headings."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def resolve_model_from_title(title: str, make: str, models):
    """Return the most-specific known model matching a listing's title, if any."""
    normalized_title = normalized_label(title)
    normalized_make = normalized_label(make)
    if not normalized_title.startswith(normalized_make):
        return None

    model_part = normalized_title[len(normalized_make):]
    matches = [
        candidate for candidate in models
        if model_part.startswith(normalized_label(candidate))
    ]
    return max(matches, key=lambda candidate: len(normalized_label(candidate))) \
        if matches else None


# "/ mėn.", "/mėn", "per mėn" — a monthly payment, not a sale price.
# Separator spacing varies and get_text() adds more, so allow whitespace.
MONTHLY_AFTER_RE = re.compile(r"^\s*[/\\]?\s*(per\s+)?(mėn|men)", re.I)
# "Mėnesio įmoka nuo: 359 €" — the giveaway sits BEFORE the number instead.
MONTHLY_BEFORE_RE = re.compile(
    r"(mėnesio|menesio|įmoka|imoka|lizing|išsimokėtinai|issimoketinai"
    r"|ilgalaikė nuoma|ilgalaike nuoma|nuomos)\D{0,40}$", re.I)


def parse_price(text):
    """
    Pick the SALE price, refusing monthly leasing figures.

    Cards come in three shapes:
      "22 750 €"                          -> 22750
      "22 750 € nuo 259 € / mėn."         -> 22750   (teaser ignored)
      "Mėnesio įmoka nuo: 359 € / mėn."   -> None    (lease-only, no sale price)

    Returning None for the third case is deliberate: a lease ad has no sale
    price, and inventing one poisons every average it lands in.
    """
    for match in re.finditer(r"(\d{1,3}(?:[\s., ]\d{3})*|\d+)\s*(?:€|EUR\b)", text):
        if MONTHLY_AFTER_RE.match(text[match.end():match.end() + 14]):
            continue
        if MONTHLY_BEFORE_RE.search(text[max(0, match.start() - 60):match.start()]):
            continue
        value = clean_number(match.group(1))
        if value and value >= 50:          # ignore stray small numbers
            return value
    return None


def from_slug(link: str):
    """
    Fall back to the URL slug, which encodes model / engine / body / year / fuel:
        bmw-320-2-0-l-sedanas-2007-dyzelinas-31767371.html
    Returns (title, year, fuel, engine_l) with None where unknown.
    """
    slug = urlparse(link).path.rsplit("/", 1)[-1].removesuffix(".html")
    tokens = slug.split("-")[:-1]          # drop the trailing ad id
    year = None
    for tok in tokens:
        if re.fullmatch(r"(19|20)\d{2}", tok):
            year = int(tok)
    fuel = None
    joined = "-".join(tokens)
    for key in FUEL_KEYS:
        if key in joined:
            fuel = FUELS[key]
            break
    engine = None
    m = re.search(r"(\d)-(\d)-l", joined)
    if m:
        engine = f"{m.group(1)}.{m.group(2)}"
    # title = everything before the engine size / body / year
    title_tokens = []
    for tok in tokens:
        if re.fullmatch(r"(19|20)\d{2}", tok) or re.fullmatch(r"\d", tok) or tok == "l":
            break
        title_tokens.append(tok)
    title = " ".join(t.capitalize() for t in title_tokens) if title_tokens else None
    return title, year, fuel, engine


def parse_listing(anchor, link: str, ad_id: str):
    """Extract one car from a result-page anchor."""
    # the whole card is normally the <a>; if not, climb to the block holding the price
    node = anchor
    for _ in range(3):
        if "€" in node.get_text():
            break
        parent = node.parent
        # never climb past the card into the page body, or we'd mix cars up
        if parent is None or parent.name in ("body", "html", "[document]"):
            break
        if len(parent.find_all("a", href=AD_HREF_RE)) > 1:
            break
        node = parent
    text = node.get_text(" ", strip=True)

    slug_title, slug_year, slug_fuel, slug_engine = from_slug(link)

    title_el = node.select_one(
        ".announcement-title, .announcement-title-wrapper, h2, h3"
    )
    title = title_el.get_text(" ", strip=True) if title_el else None
    if not title:
        title = anchor.get("title") or slug_title

    # year: "2007-05" or plain "2007" in the parameters line
    year = None
    m = re.search(r"\b((?:19|20)\d{2})(?:-\d{2})?\b", text)
    if m:
        candidate = int(m.group(1))
        if 1900 <= candidate <= 2100:
            year = candidate
    if year is None:
        year = slug_year

    price = parse_price(text)

    mileage = None
    m = re.search(r"(\d[\d\s.,]*)\s*km", text, re.IGNORECASE)
    if m:
        mileage = clean_number(m.group(1))

    engine = slug_engine
    m = re.search(r"(\d[.,]\d)\s*l\.", text, re.IGNORECASE)
    if m:
        engine = m.group(1).replace(",", ".")

    fuel = slug_fuel
    low = text.lower()
    for key in FUEL_KEYS:
        if key in low:
            fuel = FUELS[key]
            break

    # "2.0 l., 135 kW" — power separates a 320d at 120 kW from one at 140 kW,
    # which engine litres alone cannot.
    power_kw = None
    m = re.search(r"\b(\d{2,4})\s*kW\b", text)
    if m:
        power_kw = int(m.group(1))

    gearbox = next((name for key, name in GEARBOXES.items() if key in low), None)

    # Heading line is "2011-11 · Universalas": whatever is not the date is the body.
    body = None
    for span in node.select(".announcement-title-parameters span"):
        value = span.get_text(" ", strip=True)
        if value and not re.fullmatch(r"(19|20)\d{2}(-\d{2})?", value):
            body = value
            break

    # The parameter line ends with the town: the one span that is none of
    # fuel / gearbox / engine / mileage.
    location = None
    spans = [s.get_text(" ", strip=True) for s in
             node.select(".announcement-parameters-block .announcement-parameters span")]
    if spans:
        last = spans[-1]
        last_low = last.lower()
        if not (re.search(r"\d", last) or last_low in GEARBOXES
                or any(k in last_low for k in FUEL_KEYS)):
            location = last

    # Dealer cards carry a partner logo / "Visi partnerio pasiūlymai" block.
    owner = node.select_one(".announcement-owner-container")
    dealer = 1 if owner is not None and owner.get_text(strip=True) else 0

    return {
        "id": ad_id,
        "title": (title or "").strip(),
        "year": year or "",
        "price_eur": price if price is not None else "",
        "mileage_km": mileage if mileage is not None else "",
        "fuel": fuel or "",
        "engine_l": engine or "",
        "link": link,
        "power_kw": power_kw,
        "gearbox": gearbox,
        "body": body,
        "location": location,
        "dealer": dealer,
    }


def scrape_page(html: str):
    """Yield one dict per car found on a results page."""
    soup = BeautifulSoup(html, "html.parser")
    seen_on_page = set()
    for anchor in soup.find_all("a", href=True):
        m = AD_HREF_RE.search(anchor["href"])
        if not m:
            continue
        ad_id = m.group(1)
        if ad_id in seen_on_page:
            continue
        seen_on_page.add(ad_id)
        link = urljoin(BASE, anchor["href"].split("?")[0])
        row = parse_listing(anchor, link, ad_id)
        if row["price_eur"] == "" and row["year"] == "":
            continue                      # not a real listing card
        yield row


STOP = threading.Event()


def _install_sigint_handler():
    """First Ctrl-C stops politely and keeps the data; second one exits hard."""
    def handler(signum, frame):
        if STOP.is_set():
            raise KeyboardInterrupt
        STOP.set()
        print("\n! Interrupted — finishing the pages in flight. Everything scraped "
              "so far is already saved. Press Ctrl-C again to quit immediately.",
              file=sys.stderr)
    try:
        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):
        pass


# =========================================================================== #
# Database
# =========================================================================== #
SITEMAP_MODELS = f"{BASE}/xml_sitemap/makes_and_models.xml"
MODEL_PATH_RE = re.compile(r"/skelbimai/naudoti-automobiliai/([^/]+)/([^/]+)/?$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id           TEXT PRIMARY KEY,
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
    description_checked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_make_model ON listings(make, model);
CREATE INDEX IF NOT EXISTS idx_year       ON listings(year);
CREATE INDEX IF NOT EXISTS idx_last_seen  ON listings(last_seen);

CREATE TABLE IF NOT EXISTS price_history (
    id        TEXT,
    seen_at   TEXT,
    price_eur INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ph_id ON price_history(id);

CREATE TABLE IF NOT EXISTS models (
    url          TEXT PRIMARY KEY,
    make         TEXT,
    model        TEXT,
    last_scraped TEXT,
    found        INTEGER,
    was_capped   INTEGER DEFAULT 0
);
"""


# Columns added after the first schema. NULL always means "not collected yet",
# never "no": a listing scraped before power was parsed has power_kw NULL, and
# damaged is NULL until a damaged-only search has covered its model.
EXTRA_COLUMNS = {
    "description": "TEXT",
    "description_checked_at": "TEXT",
    "power_kw": "INTEGER",
    "gearbox": "TEXT",
    "body": "TEXT",
    "location": "TEXT",
    "dealer": "INTEGER",
    "damaged": "INTEGER",          # 1 = in the site's damaged-only search, 0 = not
    "damage_type": "TEXT",         # the ad page's own "Defektai" value
    "reg_country": "TEXT",         # "Pirmosios registracijos šalis"
}


def ensure_description_columns(connection):
    """Migrate databases created before descriptions, card details and damage
    flags were collected. Kept under its old name: the dashboard calls it."""
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(listings)")
    }
    for name, kind in EXTRA_COLUMNS.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE listings ADD COLUMN {name} {kind}")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_description_missing "
        "ON listings(description_checked_at)"
    )
    connection.commit()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """SQLite wrapper. One connection, one lock — writes are batched per page."""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The watcher, the dashboard and a manual run can all have the file open:
        # WAL lets readers work during a write, and the timeout makes a writer
        # wait its turn instead of failing with "database is locked".
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        ensure_description_columns(self.conn)
        self.conn.commit()
        self.lock = threading.Lock()
        self.inserted = 0
        self.updated = 0
        self.price_changes = 0

    def upsert_many(self, rows, make, model):
        if not rows:
            return
        ts = now_iso()
        with self.lock:
            cur = self.conn.cursor()
            for r in rows:
                price = r["price_eur"] if r["price_eur"] != "" else None
                row_make = r.get("_make", make)
                row_model = r.get("_model", model)
                cur.execute(
                    "SELECT price_eur, make, model FROM listings WHERE id = ?",
                    (r["id"],),
                )
                existing = cur.fetchone()
                card = (r.get("power_kw"), r.get("gearbox"), r.get("body"),
                        r.get("location"), r.get("dealer"))
                if existing is None:
                    cur.execute(
                        """INSERT INTO listings
                           (id, make, model, title, year, price_eur, mileage_km,
                            fuel, engine_l, link, first_seen, last_seen, seen_count,
                            power_kw, gearbox, body, location, dealer)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)""",
                        (r["id"], row_make, row_model, r["title"],
                         r["year"] or None, price,
                         r["mileage_km"] if r["mileage_km"] != "" else None,
                         r["fuel"], r["engine_l"], r["link"], ts, ts, *card))
                    if price is not None:
                        cur.execute("INSERT INTO price_history VALUES (?,?,?)",
                                    (r["id"], ts, price))
                    self.inserted += 1
                else:
                    old_price = existing[0]
                    # Write the price we actually parsed, even when it's NULL.
                    # COALESCE here would preserve a previously mis-parsed price
                    # (e.g. a lease payment) forever, so a re-scrape could never
                    # repair it. price_history keeps the old value regardless.
                    # Card details only ever fill in: a source that doesn't parse
                    # them (the watcher CSV) must not blank what a full scrape found.
                    cur.execute(
                        """UPDATE listings
                           SET last_seen = ?, seen_count = seen_count + 1,
                               price_eur = ?, make = ?, model = ?,
                               power_kw = COALESCE(?, power_kw),
                               gearbox  = COALESCE(?, gearbox),
                               body     = COALESCE(?, body),
                               location = COALESCE(?, location),
                               dealer   = COALESCE(?, dealer)
                           WHERE id = ?""",
                        (ts, price, row_make, row_model, *card, r["id"]))
                    if price is not None and price != old_price:
                        cur.execute("INSERT INTO price_history VALUES (?,?,?)",
                                    (r["id"], ts, price))
                        self.price_changes += 1
                    self.updated += 1
            self.conn.commit()

    def mark_model(self, url, make, model, found, capped):
        with self.lock:
            self.conn.execute(
                """INSERT INTO models (url, make, model, last_scraped, found, was_capped)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET
                     last_scraped = excluded.last_scraped,
                     found = excluded.found,
                     was_capped = excluded.was_capped""",
                (url, make, model, now_iso(), found, 1 if capped else 0))
            self.conn.commit()

    def missing_descriptions(self, limit=None, ids=None):
        """Unchecked listings, oldest first — or, given `ids`, only those, in
        the order given (the stats candidate file is best-deal first)."""
        with self.lock:
            if ids is not None:
                rows = []
                for ad_id in ids:
                    row = self.conn.execute(
                        """SELECT id, link FROM listings
                           WHERE id = ? AND description_checked_at IS NULL""",
                        (ad_id,)).fetchone()
                    if row:
                        rows.append(row)
                return rows[:limit] if limit is not None else rows
            query = """SELECT id, link FROM listings
                       WHERE description_checked_at IS NULL
                       ORDER BY first_seen, id"""
            if limit is None:
                rows = self.conn.execute(query).fetchall()
            else:
                rows = self.conn.execute(query + " LIMIT ?", (limit,)).fetchall()
        return rows

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
                   WHERE id = ?""",
                (description, now_iso(), details.get("power_kw"),
                 details.get("gearbox"), details.get("body"), damage, damage,
                 details.get("reg_country"), ad_id),
            )
            self.conn.commit()

    def mark_damage(self, make, model, damaged_ids):
        """Record one complete damaged-only search for a model: its ids are
        damaged, every other listing of that model is not."""
        with self.lock:
            self.conn.execute(
                "UPDATE listings SET damaged = 0 WHERE make = ? AND model = ?",
                (make, model))
            self.conn.executemany(
                "UPDATE listings SET damaged = 1 WHERE id = ?",
                [(i,) for i in damaged_ids])
            self.conn.commit()

    def descriptions_remaining(self):
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM listings WHERE description_checked_at IS NULL"
            ).fetchone()[0]

    def save_models(self, entries):
        with self.lock:
            self.conn.executemany(
                """INSERT INTO models (url, make, model, last_scraped, found)
                   VALUES (?,?,?,NULL,NULL)
                   ON CONFLICT(url) DO NOTHING""", entries)
            self.conn.commit()

    def pending_models(self, max_age_days, force):
        with self.lock:
            if force:
                rows = self.conn.execute(
                    "SELECT url, make, model FROM models ORDER BY make, model").fetchall()
            else:
                rows = self.conn.execute(
                    """SELECT url, make, model FROM models
                       WHERE last_scraped IS NULL
                          OR julianday('now') - julianday(last_scraped) > ?
                       ORDER BY make, model""", (max_age_days,)).fetchall()
        return rows

    def models_for_make(self, make):
        with self.lock:
            return [
                row[0] for row in self.conn.execute(
                    "SELECT model FROM models WHERE make = ? ORDER BY model", (make,)
                )
            ]

    def counts(self):
        with self.lock:
            total = self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
            models_total = self.conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
            models_done = self.conn.execute(
                "SELECT COUNT(*) FROM models WHERE last_scraped IS NOT NULL").fetchone()[0]
        return total, models_done, models_total

    def close(self):
        with self.lock:
            self.conn.close()


def band_url(base, lo, hi):
    parts = urlparse(base)
    q = parse_qs(parts.query)
    q["price_from"] = [str(lo)]
    q["price_to"] = [str(hi)]
    q.pop("page_nr", None)
    return urlunparse(parts._replace(query=urlencode(q, doseq=True)))


def probe(session, url, timeout=20):
    """Single request, returns (status_code_or_None, body_or_error_text)."""
    try:
        r = session.get(url, headers=HEADERS, timeout=timeout)
        return r.status_code, r.text[:4000]
    except requests.RequestException as exc:
        return None, f"{exc.__class__.__name__}: {exc}"


def do_diagnose(session):
    """Check which parts of the site answer THIS machine, and say what's wrong."""
    targets = [
        ("robots.txt",            f"{BASE}/robots.txt"),
        ("main search page",      f"{BASE}/skelbimai/naudoti-automobiliai"),
        ("a model page (bmw/320)", f"{BASE}/skelbimai/naudoti-automobiliai/bmw/320"),
        ("model list sitemap",    SITEMAP_MODELS),
    ]
    print("Checking what answers this machine:\n")
    ok = []
    for label, url in targets:
        status, body = probe(session, url)
        if status == 200:
            ok.append(label)
            print(f"  OK    {label}")
        else:
            shown = status if status else body
            print(f"  FAIL  {label}  ->  {shown}")
            if status and isinstance(body, str) and body.strip():
                print(f"          looks like: {diagnose(body)}")
    print()
    if not ok:
        print("Nothing answered at all. That is an IP-level refusal, not a bug in")
        print("this script. If you are on a VPN, turn it off and try again — VPN and")
        print("datacenter IPs get challenged on sight regardless of how slowly you ask.")
    elif len(ok) < len(targets):
        print("Some paths answer and others don't, so your IP is fine — one specific")
        print("path is blocked. Use --models-file to supply the model list by hand and")
        print("skip the blocked path entirely (see --help for the format).")
    else:
        print("Everything answered. Re-run the scrape.")


def load_models_file(path):
    """Read 'make/model' lines, so a blocked sitemap can't stop you."""
    entries = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "/" not in line:
            continue
        make, model = line.split("/", 1)
        make, model = make.strip().lower(), model.strip().lower()
        if make and model:
            entries.append(
                (f"{BASE}/skelbimai/naudoti-automobiliai/{make}/{model}", make, model))
    return entries


def fetch_models_from_html(session, blocks, delay=1.0):
    """Fallback: harvest make/model links from the site's own pages."""
    print("Sitemap unavailable — reading the model list from the site's pages instead.")
    html = fetch(session, f"{BASE}/skelbimai/naudoti-automobiliai", blocks=blocks)
    if not html:
        return []

    def links(page_html):
        found_models, found_makes = set(), set()
        soup = BeautifulSoup(page_html, "html.parser")
        for a in soup.find_all("a", href=True):
            path = urlparse(a["href"]).path.rstrip("/")
            m = MODEL_PATH_RE.search(path + "/")
            if m:
                found_models.add((m.group(1), m.group(2)))
                continue
            m2 = re.search(r"/skelbimai/naudoti-automobiliai/([^/]+)$", path)
            if m2 and m2.group(1) != "naudoti-automobiliai":
                found_makes.add(m2.group(1))
        return found_models, found_makes

    models, makes = links(html)
    if not models and makes:
        print(f"  found {len(makes)} makes, fetching their model lists...")
        for i, mk in enumerate(sorted(makes), start=1):
            if STOP.is_set() or blocks.tripped.is_set():
                break
            sub = fetch(session, f"{BASE}/skelbimai/naudoti-automobiliai/{mk}",
                        blocks=blocks)
            if sub:
                found, _ = links(sub)
                models |= found
            if i % 10 == 0:
                print(f"  ...{i}/{len(makes)} makes, {len(models)} models so far")
            time.sleep(delay)

    return [(f"{BASE}/skelbimai/naudoti-automobiliai/{mk}/{md}", mk, md)
            for mk, md in sorted(models)]


def fetch_model_list(session, blocks):
    """Pull make/model search URLs from the site's own sitemap."""
    print(f"Fetching model list from {SITEMAP_MODELS}")
    xml = fetch(session, SITEMAP_MODELS, blocks=blocks)
    if not xml:
        return []
    entries, seen = [], set()
    for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml):
        m = MODEL_PATH_RE.search(urlparse(loc).path)
        if not m:
            continue
        make, model = m.group(1), m.group(2)
        url = f"{BASE}/skelbimai/naudoti-automobiliai/{make}/{model}"
        if url in seen:
            continue
        seen.add(url)
        entries.append((url, make, model))
    return entries


def scrape_search(url, make, model, store, session, blocks, args, label=""):
    """
    Walk pages of one search URL, upserting each page.

    Stops when a batch of pages yields NO NEW ids. That's the only reliable
    end-of-results signal: autoplius clamps out-of-range page numbers and
    re-serves page 1, so pages never come back empty — they come back full of
    listings you've already seen.

    Note `ids` is per-search, not global, so "no new ids" can't be confused
    with "already in the database from a previous run".

    Returns (ids_seen, complete) — complete is False if we were cut short by a
    block or Ctrl-C, so the caller knows not to mark the model as done.
    """
    ids = set()
    page = 1
    complete = False
    conc = max(1, args.concurrency)
    known_models = store.models_for_make(make)

    while page <= args.max_pages:
        if STOP.is_set() or blocks.tripped.is_set():
            break
        batch = list(range(page, min(page + conc, args.max_pages + 1)))
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            htmls = list(pool.map(
                lambda n: fetch(session, page_url(url, n), blocks=blocks), batch))

        if not any(htmls):
            print(f"    {label}pages {batch[0]}-{batch[-1]}: no response — stopping")
            break

        before = len(ids)
        saw_empty = False
        rejected = 0
        matching = 0
        for html in htmls:
            if not html:
                continue
            page_rows = list(scrape_page(html))
            if not page_rows:
                saw_empty = True
                continue
            rows = []
            for row in page_rows:
                matched_model = resolve_model_from_title(
                    row.get("title", ""), make, known_models
                )
                if matched_model != model:
                    rejected += 1
                    continue
                row["_make"] = make
                row["_model"] = matched_model
                rows.append(row)
            if not rows:
                continue
            matching += len(rows)
            fresh = [r for r in rows if r["id"] not in ids]
            ids.update(r["id"] for r in rows)
            store.upsert_many(fresh, make, model)

        gained = len(ids) - before
        if rejected:
            print(f"    {label}ignored {rejected} listing(s) whose title did not "
                  f"match {make}/{model}")
        if matching == 0 and not saw_empty:
            print(f"    {label}no listings matched {make}/{model}; "
                  "stopping this model rather than saving unrelated results")
            complete = True
            break
        print(f"    {label}pages {batch[0]}-{batch[-1]}: +{gained} matching "
              f"({len(ids)} total)")

        if gained == 0 or saw_empty:
            complete = True
            break

        page += conc
        time.sleep(args.delay)
    else:
        complete = True          # walked the whole allowed range without stopping

    return ids, complete


def damaged_url(url):
    q = "&".join(f"has_damaged_id[{i}]={i}" for i in DAMAGE_IDS)
    return url + ("&" if "?" in url else "?") + q


def result_count(html):
    """The '(17)' total the site prints over a result list, or None."""
    m = re.search(r'class="result-count"[^>]*>\s*\(?\s*([\d\s ]+)\)', html or "")
    return clean_number(m.group(1)) if m else None


AGE_UNITS = {"min": 1, "val": 60, "d": 1440, "sav": 10080, "mėn": 43200}


def parse_age(text):
    """Minutes since publishing, from a card's badge: "Prieš 1 val. 15 min." -> 75,
    "Mažiau nei prieš minutę" -> 0. None for anything else (older ads carry no
    badge at all)."""
    text = (text or "").strip().lower()
    if not text:
        return None
    if "mažiau nei" in text:
        return 0
    parts = re.findall(r"(\d+)\s*(min|val|d|sav|mėn)\b", text)
    return sum(int(n) * AGE_UNITS[unit] for n, unit in parts) if parts else None


def card_ages(html):
    """{ad id: minutes since published} for every card on a results page that
    shows an age badge."""
    ages = {}
    for card in BeautifulSoup(html or "", "html.parser").select("a.announcement-item"):
        m = AD_HREF_RE.search(card.get("href", ""))
        badge = card.select_one(".badge-new")
        if m and badge:
            age = parse_age(badge.get_text(" ", strip=True))
            if age is not None:
                ages[m.group(1)] = age
    return ages


def scrape_damaged_ids(url, make, model, store, session, blocks, args):
    """
    IDs of this model's listings that the site files under a declared defect.

    Nothing is upserted: these cars are already in the main pass, and counting
    them twice would bump seen_count and fake a 'seen in two scrapes' duration.
    Returns None if the search was cut short, so a partial list can never mark
    the rest of the model clean.
    """
    known_models = store.models_for_make(make)
    search = damaged_url(url)
    ids = set()
    total = None
    for page in range(1, args.max_pages + 1):
        if STOP.is_set() or blocks.tripped.is_set():
            return None
        html = fetch(session, page_url(search, page), blocks=blocks)
        if not html:
            return None
        if page == 1:
            total = result_count(html)
            if total == 0:       # an empty search may be padded with suggestions
                return ids
        before = len(ids)
        for row in scrape_page(html):
            if resolve_model_from_title(row.get("title", ""), make, known_models) == model:
                ids.add(row["id"])
        if len(ids) == before or (total is not None and len(ids) >= total):
            break
        time.sleep(args.delay)
    return ids


def scrape_model(url, make, model, store, session, blocks, args):
    """Scrape one model; if it hits the pagination cap, split it by price."""
    ids, complete = scrape_search(url, make, model, store, session, blocks, args)
    capped = len(ids) >= args.cap_threshold

    if capped:
        print(f"    {make}/{model} hit the cap ({len(ids)}) — splitting by price")
        queue = [(0, 9999999)]
        while queue:
            if STOP.is_set() or blocks.tripped.is_set():
                break
            lo, hi = queue.pop(0)
            sub, sub_ok = scrape_search(band_url(url, lo, hi), make, model, store,
                                        session, blocks, args, label=f"EUR {lo}-{hi} ")
            ids |= sub
            complete = complete and sub_ok
            if len(sub) >= args.cap_threshold and (hi - lo) > args.min_width:
                mid = lo + (hi - lo) // 2
                queue.insert(0, (mid + 1, hi))
                queue.insert(0, (lo, mid))

    if complete and not STOP.is_set() and not blocks.tripped.is_set():
        damaged = scrape_damaged_ids(url, make, model, store, session, blocks, args)
        if damaged is not None:
            store.mark_damage(make, model, damaged)
            print(f"    {len(damaged)} listed with a declared defect")
        store.mark_model(url, make, model, len(ids), capped)
    else:
        print(f"    {make}/{model} was cut short — left unmarked so the next "
              f"run retries it")
    return ids


def do_refresh_damage(store, session, blocks, args, todo):
    """Damaged-only searches for already-scraped models, without re-scraping them."""
    print(f"{len(todo)} model(s) to check for declared defects\n")
    flagged = 0
    for i, (url, make, model) in enumerate(todo, start=1):
        if STOP.is_set() or blocks.tripped.is_set():
            break
        damaged = scrape_damaged_ids(url, make, model, store, session, blocks, args)
        if damaged is None:
            print(f"[{i}/{len(todo)}] {make}/{model}: cut short, left unchanged")
            continue
        store.mark_damage(make, model, damaged)
        flagged += len(damaged)
        if damaged:
            print(f"[{i}/{len(todo)}] {make}/{model}: {len(damaged)} with a declared defect")
        time.sleep(args.delay)
    print(f"\nDamaged listings flagged: {flagged}")


def load_ids_file(path):
    with open(path, encoding="utf-8") as fh:
        return [line.split()[0] for line in fh
                if line.strip() and not line.lstrip().startswith("#")]


def fetch_missing_descriptions(store, session, blocks, delay, limit=None, ids=None):
    """Fetch descriptions (and ad-page details) for unchecked listings and save
    progress per ad. With `ids`, only those — e.g. data/candidates.txt."""
    todo = store.missing_descriptions(limit, ids)
    print(f"{len(todo)} listing description(s) queued.")
    checked = 0
    for index, (ad_id, link) in enumerate(todo, start=1):
        if STOP.is_set() or blocks.tripped.is_set():
            break
        if not link:
            print(f"[{index}/{len(todo)}] {ad_id}: no listing URL; skipping")
            store.save_description(ad_id, None)
            continue
        try:
            description, details, completed, status = fetch_ad_details(
                session, link, blocks
            )
        except ValueError as exc:
            print(f"[{index}/{len(todo)}] {ad_id}: {exc}", file=sys.stderr)
            store.save_description(ad_id, None)
            continue
        if not completed:
            if status == 200:
                print("Stopped on a browser verification challenge.")
            else:
                print("Stopped after a request failure; remaining ads are "
                      "left queued for a later run.")
            break
        store.save_description(ad_id, description, details)
        checked += 1
        result = "description saved" if description else "no description"
        if details.get("damage_type"):
            result += f" · defect: {details['damage_type']}"
        print(f"[{index}/{len(todo)}] {ad_id}: {result}")
        if index < len(todo) and delay:
            time.sleep(delay)
    print(f"Descriptions checked this run: {checked}")
    print(f"Descriptions still queued: {store.descriptions_remaining()}")
    return checked


def do_export(store, path):
    cols = ["id", "make", "model", "title", "year", "price_eur", "mileage_km",
            "fuel", "engine_l", "link", "first_seen", "last_seen", "seen_count"]
    with store.lock:
        rows = store.conn.execute(f"SELECT {','.join(cols)} FROM listings").fetchall()
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerows(rows)
    print(f"Exported {len(rows)} listings to {path}")


def find_gone(store):
    """
    A listing is GONE if its model's last full scrape finished more than an
    hour after the listing was last seen, i.e. that scrape walked the whole
    model and didn't find it. Same definition as autoplius_stats.py's
    mark_disappeared(), so "gone" means the same thing everywhere.

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
        scraped = {(mk, md): ts(at) for mk, md, at in store.conn.execute(
            "SELECT make, model, last_scraped FROM models")}
        rows = store.conn.execute(
            "SELECT id, make, model, last_seen FROM listings").fetchall()
    gone = []
    for rid, make, model, last_seen in rows:
        seen, ref = ts(last_seen), scraped.get((make, model))
        if seen and ref and (ref - seen).total_seconds() > 3600:
            gone.append(rid)
    return gone


def do_prune_gone(store, dry_run):
    ids = find_gone(store)
    total = store.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    if not ids:
        print("Nothing to prune — no listing is behind its model's most recent scrape.")
        return
    print(f"{len(ids)} of {total} listings are gone (not seen in their model's "
          f"most recent scrape).")
    if dry_run:
        print("Dry run — nothing deleted. Re-run without --dry-run to remove them.")
        return
    with store.lock:
        cur = store.conn.cursor()
        cur.executemany("DELETE FROM price_history WHERE id = ?", [(i,) for i in ids])
        cur.executemany("DELETE FROM listings WHERE id = ?", [(i,) for i in ids])
        store.conn.commit()
    remaining = store.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    print(f"Deleted {len(ids)} listings (and their price history). "
          f"{remaining} listings remain.")


def do_status(store):
    total, done, models_total = store.counts()
    print(f"Listings stored : {total}")
    print(f"Models scraped  : {done} / {models_total}")
    with store.lock:
        top = store.conn.execute(
            """SELECT make, model, COUNT(*) c FROM listings
               GROUP BY make, model ORDER BY c DESC LIMIT 15""").fetchall()
        capped = store.conn.execute(
            "SELECT COUNT(*) FROM models WHERE was_capped = 1").fetchone()[0]
    if capped:
        print(f"Models that needed price-splitting: {capped}")
    if top:
        print("\nBiggest model groups (this is where to spend generation-mapping effort):")
        for make, model, c in top:
            print(f"  {c:6d}  {make}/{model}")


def main():
    ap = argparse.ArgumentParser(description="Build the autoplius baseline dataset")
    ap.add_argument("--db", default=str(project_path(SETTINGS["paths"]["database"])),
                    help="SQLite file (default: data/autoplius.db)")
    ap.add_argument("--concurrency", type=int, default=CFG["concurrency"])
    ap.add_argument("--delay", type=float, default=CFG["delay"])
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--cap-threshold", type=int, default=3900,
                    help="a search returning this many is assumed capped")
    ap.add_argument("--min-width", type=int, default=50)
    ap.add_argument("--limit", type=int, default=None,
                    help="scrape at most this many models this run")
    ap.add_argument("--max-age", type=float, default=CFG["max_age_days"],
                    help="re-scrape a model if older than this many days")
    ap.add_argument("--force", action="store_true", help="re-scrape everything")
    ap.add_argument("--models", default=None,
                    help='only these, e.g. "bmw/320,audi/a4"')
    ap.add_argument("--refresh-models", action="store_true",
                    help="re-fetch the make/model list from the sitemap")
    ap.add_argument("--status", action="store_true", help="show progress and exit")
    ap.add_argument("--prune-gone", action="store_true",
                    help="delete listings not seen in their model's most recent "
                         "scrape (and their price history), then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --prune-gone, report what would be deleted without deleting")
    ap.add_argument("--diagnose", action="store_true",
                    help="check what the site will serve this machine, then exit")
    ap.add_argument("--models-file", default=None,
                    help="text file of 'make/model' lines instead of the sitemap "
                         "(example: data/models.txt)")
    ap.add_argument("--export", default=None, help="export listings to CSV and exit")
    ap.add_argument("--block-threshold", type=int, default=CFG["block_threshold"])
    ap.add_argument("--backfill-descriptions", action="store_true",
                    help="fetch and save seller descriptions for listings not checked yet")
    ap.add_argument("--description-delay", type=float, default=CFG["description_delay"],
                    help="seconds between description requests (default: 2)")
    ap.add_argument("--description-limit", type=int, default=None,
                    help="check at most this many descriptions this run")
    ap.add_argument("--ids-file", default=None,
                    help="with --backfill-descriptions, fetch only these ad IDs, in "
                         "order (autoplius_stats.py writes data/candidates.txt)")
    ap.add_argument("--all-descriptions", action="store_true",
                    help="after scraping, fetch every missing description (one "
                         "request per ad — tens of hours on a full database)")
    ap.add_argument("--refresh-damage", action="store_true",
                    help="run only the damaged-only search for each scraped model "
                         "(or --models), flagging cars with a declared defect")
    args = ap.parse_args()

    if args.description_delay < 0:
        ap.error("--description-delay must be zero or greater")
    if args.description_limit is not None and args.description_limit < 1:
        ap.error("--description-limit must be at least 1")

    _install_sigint_handler()
    store = Store(args.db)

    if args.backfill_descriptions:
        session = requests.Session()
        blocks = BlockDetector(threshold=1)
        ids = load_ids_file(args.ids_file) if args.ids_file else None
        try:
            fetch_missing_descriptions(
                store, session, blocks, args.description_delay,
                args.description_limit, ids,
            )
        finally:
            session.close()
        store.close()
        return

    if args.diagnose:
        do_diagnose(requests.Session()); store.close(); return
    if args.status:
        do_status(store); store.close(); return
    if args.export:
        do_export(store, args.export); store.close(); return
    if args.prune_gone:
        do_prune_gone(store, args.dry_run); store.close(); return

    session = requests.Session()
    blocks = BlockDetector(threshold=args.block_threshold)

    _, _, models_total = store.counts()
    if args.models_file or args.refresh_models or models_total == 0:
        if args.models_file:
            entries = load_models_file(args.models_file)
            print(f"Model list: {len(entries)} models from {args.models_file}")
        else:
            entries = fetch_model_list(session, blocks)
            if not entries and not blocks.tripped.is_set():
                entries = fetch_models_from_html(session, blocks, args.delay)
        if not entries:
            print("\nCould not build a model list.", file=sys.stderr)
            print("Run:  python scripts/autoplius_db.py --diagnose", file=sys.stderr)
            print("It will tell you whether the whole site is refusing this machine "
                  "(usually a VPN) or just one path.", file=sys.stderr)
            store.close(); sys.exit(1)
        store.save_models(entries)
        print(f"Model list: {len(entries)} models known\n")

    if args.models:
        wanted = {m.strip().lower() for m in args.models.split(",") if m.strip()}
        todo = [(u, mk, md) for (u, mk, md) in store.pending_models(0, True)
                if f"{mk}/{md}".lower() in wanted]
    else:
        todo = store.pending_models(args.max_age, args.force)

    if args.limit is not None:     # 0 legitimately means "discover the model list, scrape nothing"
        todo = todo[:args.limit]

    if args.refresh_damage:
        if not args.models:
            with store.lock:
                todo = store.conn.execute(
                    """SELECT url, make, model FROM models
                       WHERE last_scraped IS NOT NULL ORDER BY make, model""").fetchall()
            if args.limit is not None:
                todo = todo[:args.limit]
        try:
            do_refresh_damage(store, session, blocks, args, todo)
        finally:
            store.close()
            session.close()
        return

    if not todo:
        print("Nothing to scrape — everything is up to date. "
              "Use --force or --max-age to redo models.")
    else:
        print(f"{len(todo)} model(s) to scrape\n")

    try:
        for i, (url, make, model) in enumerate(todo, start=1):
            if STOP.is_set() or blocks.tripped.is_set():
                break
            print(f"[{i}/{len(todo)}] {make}/{model}")
            scrape_model(url, make, model, store, session, blocks, args)
            total, done, mtotal = store.counts()
            print(f"    done — db now holds {total} listings\n")
        if args.all_descriptions and not STOP.is_set() and not blocks.tripped.is_set():
            fetch_missing_descriptions(
                store, session, blocks, args.description_delay,
                args.description_limit,
            )
        elif todo:
            print("Descriptions are fetched for candidates only: run "
                  "autoplius_stats.py, then\n  autoplius_db.py --backfill-descriptions "
                  "--ids-file data/candidates.txt")
    except KeyboardInterrupt:
        STOP.set()
    finally:
        total, done, mtotal = store.counts()
        print("=" * 68)
        print(f"New listings inserted : {store.inserted}")
        print(f"Existing refreshed    : {store.updated}")
        print(f"Price changes logged  : {store.price_changes}")
        print(f"Database total        : {total} listings")
        print(f"Models scraped        : {done} / {mtotal}")
        print("=" * 68)
        if blocks.tripped.is_set():
            print("\nThe site was refusing requests when we stopped.", file=sys.stderr)
            if blocks.dump_path.exists():
                body = blocks.dump_path.read_text(encoding="utf-8", errors="replace")
                print(f"Looks like: {diagnose(body)}", file=sys.stderr)
            print("Wait a while, then re-run — finished models are skipped.", file=sys.stderr)
        store.close()
        session.close()


if __name__ == "__main__":
    main()
