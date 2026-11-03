#!/usr/bin/env python3
"""
autoplius_scraper.py — scrape car listings from autoplius.lt into a CSV.

For each listing it collects: title (make + model), year, price (EUR),
mileage, fuel, engine size and the direct link to the ad.

Usage
-----
    pip install requests beautifulsoup4

    # default: first 3 pages of all used cars
    python scripts/autoplius_scraper.py

    # a specific search (copy the URL from your browser after filtering on the site)
    python scripts/autoplius_scraper.py \
        --url "https://autoplius.lt/skelbimai/naudoti-automobiliai?make_id=99&model_id=1266" \
        --pages 10 -o bmw.csv

Options
-------
    --url          search results URL from autoplius.lt (any filters you set are kept)
    --pages        how many result pages to walk through (default 3)
    --delay        seconds between requests within one worker (default 1.0)
    --concurrency  how many pages to fetch in parallel (default 1). This is plain
                   HTTP requests, no browser, so it's the fastest way to scrape —
                   but also the easiest way to get rate-limited if you push it too
                   high. 4-8 is plenty; each is still spaced out by --delay.
    -o             output CSV file (default data/autoplius_cars.csv)

Note: this reads publicly visible pages only. Keep the delay reasonable so you
don't hammer the site, and check autoplius.lt's terms before doing anything
large-scale or commercial with the data.
"""

import argparse
import csv
import random
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

from settings import SETTINGS, project_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = project_path(SETTINGS["paths"]["data_dir"])
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

FIELDS = ["id", "title", "year", "price_eur", "mileage_km", "fuel", "engine_l", "link"]


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
        return "a flat 'access denied' — the IP itself is blocked, not you as a user"
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


def clean_number(text: str):
    """'22 750' / '22.750' -> 22750"""
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


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

    return {
        "id": ad_id,
        "title": (title or "").strip(),
        "year": year or "",
        "price_eur": price if price is not None else "",
        "mileage_km": mileage if mileage is not None else "",
        "fuel": fuel or "",
        "engine_l": engine or "",
        "link": link,
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


class CsvSink:
    """
    Appends rows to the CSV as soon as a page is parsed and flushes straight
    away, so a crash, a block, or a Ctrl-C never costs more than the page that
    was in flight. Opening an existing file appends to it instead of wiping it.
    """

    def __init__(self, path, fields):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fields = fields
        self.lock = threading.Lock()
        self.written = 0
        fresh = not (self.path.exists() and self.path.stat().st_size > 0)
        # BOM only on a fresh file — appending with utf-8-sig would drop a stray
        # BOM into the middle of the file
        self.fh = open(self.path, "w" if fresh else "a", newline="",
                       encoding="utf-8-sig" if fresh else "utf-8")
        self.writer = csv.DictWriter(self.fh, fieldnames=fields)
        if fresh:
            self.writer.writeheader()
            self.fh.flush()

    def write(self, rows):
        if not rows:
            return
        with self.lock:
            self.writer.writerows(rows)
            self.fh.flush()
            self.written += len(rows)

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass


def load_existing_ids(path):
    """Ad ids already in the CSV, so a resumed run doesn't duplicate them."""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return set()
    ids = set()
    try:
        with open(p, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                if row.get("id"):
                    ids.add(row["id"])
    except OSError as exc:
        print(f"  ! could not read {path} to resume: {exc}", file=sys.stderr)
    return ids


def worker(worker_id, page_numbers, url, session, seen, sink, lock, delay,
           blocks=None, verbose_prefix=True, band_ids=None):
    """
    Fetch this worker's pages, writing each page's rows to disk immediately.
    If band_ids is given, every ad id seen is recorded there (even ones already
    in the CSV) so the caller can measure how big this search really is.
    """
    tag = f"[w{worker_id}] " if verbose_prefix else ""
    collected = 0
    for page in page_numbers:
        if STOP.is_set():
            print(f"{tag}stopping — interrupted")
            break
        if blocks is not None and blocks.tripped.is_set():
            print(f"{tag}stopping — the site is refusing our requests")
            break

        target = page_url(url, page)
        html = fetch(session, target, blocks=blocks)
        if not html:
            print(f"{tag}page {page}: ! no content, skipping", file=sys.stderr)
            continue

        parsed, fresh = 0, []
        for row in scrape_page(html):
            parsed += 1
            with lock:
                if band_ids is not None:
                    band_ids.add(row["id"])
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
            fresh.append(row)

        sink.write(fresh)              # <- on disk before we move on
        collected += len(fresh)
        print(f"{tag}page {page}: {parsed} listings, {len(fresh)} new "
              f"(file now has {sink.written} added)")

        # Stop only when the PAGE ITSELF is empty. "0 new" is normal on a
        # resumed run — it just means we already have those cars.
        if parsed == 0:
            print(f"{tag}page {page} is empty — end of results, stopping this worker")
            break

        time.sleep(delay + random.uniform(0, 0.4))
    return collected


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


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Scrape car listings from autoplius.lt")
    ap.add_argument("--url", default=DEFAULT_SEARCH,
                    help="search results URL (default: all used cars)")
    ap.add_argument("--pages", type=int, default=3,
                    help="how many pages to scrape this run (default 3)")
    ap.add_argument("--start-page", type=int, default=1,
                    help="first page number to scrape (default 1) — use this to "
                         "do a big scrape in chunks")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds between requests within one worker (default 1.0)")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="how many pages to fetch in parallel (default 1)")
    ap.add_argument("-o", "--output", default=str(DATA_DIR / "autoplius_cars.csv"),
                    help="CSV file (default: data/autoplius_cars.csv)")
    ap.add_argument("--fresh", action="store_true",
                    help="start a new CSV instead of resuming into the existing one")
    ap.add_argument("--block-threshold", type=int, default=8,
                    help="stop everything after this many refusals in a row (default 8)")
    args = ap.parse_args()

    _install_sigint_handler()

    out_path = Path(args.output)
    if args.fresh and out_path.exists():
        out_path.unlink()

    seen = load_existing_ids(args.output)
    if seen:
        print(f"Resuming into {args.output} — {len(seen)} cars already there "
              f"(they won't be re-added).")

    concurrency = max(1, args.concurrency)
    session = requests.Session()
    lock = threading.Lock()
    blocks = BlockDetector(threshold=args.block_threshold)
    sink = CsvSink(args.output, FIELDS)

    start = max(1, args.start_page)
    page_numbers = list(range(start, start + max(1, args.pages)))
    print(f"Scraping pages {page_numbers[0]}-{page_numbers[-1]} "
          f"with {concurrency} worker(s)\n")

    buckets = [page_numbers[i::concurrency] for i in range(concurrency)]
    buckets = [b for b in buckets if b]

    try:
        if concurrency == 1:
            worker(1, buckets[0], args.url, session, seen, sink, lock, args.delay,
                   blocks=blocks, verbose_prefix=False)
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = []
                for i, bucket in enumerate(buckets, start=1):
                    time.sleep(0.15)   # stagger so they don't fire in unison
                    futures.append(pool.submit(
                        worker, i, bucket, args.url, session, seen, sink, lock,
                        args.delay, blocks
                    ))
                for f in as_completed(futures):
                    f.result()
    except KeyboardInterrupt:
        STOP.set()
    finally:
        sink.close()

    if blocks.tripped.is_set():
        print("\n" + "=" * 68, file=sys.stderr)
        print("STOPPED: the site refused our requests repeatedly.", file=sys.stderr)
        if blocks.first_status:
            print(f"  First refusal: HTTP {blocks.first_status}", file=sys.stderr)
        if blocks.dump_path.exists():
            body = blocks.dump_path.read_text(encoding="utf-8", errors="replace")
            print(f"  Looks like: {diagnose(body)}", file=sys.stderr)
            print(f"  Saved page: {blocks.dump_path.resolve()}", file=sys.stderr)
        print("", file=sys.stderr)
        print("  If the refusals started on the very FIRST request, it's the IP you're", file=sys.stderr)
        print("  coming from being refused on sight (VPN/datacenter ranges usually are)", file=sys.stderr)
        print("  — not you going too fast. If they started only after a while, back off:", file=sys.stderr)
        print("  lower --concurrency, raise --delay, and wait before trying again.", file=sys.stderr)
        print("", file=sys.stderr)
        print(f"  Whatever was scraped before the block is already in {args.output}.", file=sys.stderr)
        print(f"  Re-run the same command later to carry on from there.", file=sys.stderr)
        print("=" * 68, file=sys.stderr)

    total = len(seen)
    print(f"\nAdded {sink.written} new cars. {args.output} now holds {total}.")
    if sink.written == 0 and not blocks.tripped.is_set() and not STOP.is_set():
        print("Nothing new was found — either you already have these pages, or the "
              "page layout changed.", file=sys.stderr)


if __name__ == "__main__":
    main()
