"""
collectors/autoplius.py — everything specific to autoplius.lt: its URLs, its
result cards and ad pages, its make/model catalogue, its damage filter.

The shared code (collectors.crawl, collectors.watch) drives a site through
these names:

    SOURCE, PAGE_SIZE
    page_url(url, n), band_url(url, lo, hi), result_count(html)
    scrape_page(html) -> card dicts, card_ages(html) -> {id: minutes}
    newest_url                      newest-first list, for the watcher
    damaged_search_urls()           searches listing every declared defect
    fetch_ad_details(session, url, blocks)
    fetch_catalogue(session, blocks), catalogue_url(entry)
    fetch_make_counts(session)      {make: listings on the site}
    diagnose_targets()
"""

import json
import re
import sys
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from collectors.common import (AD_GONE_STATUS, HEADERS, BlockDetector, clean_number,
                               fetch, name_slug, parse_age)

SOURCE = "autoplius"
PAGE_SIZE = 20

# =========================================================================== #
# Fetching + parsing engine
# =========================================================================== #
BASE = "https://autoplius.lt"

DEFAULT_SEARCH = f"{BASE}/skelbimai/naudoti-automobiliai"

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

    if response.status_code in AD_GONE_STATUS:
        # The ad was taken down (sold or withdrawn): an answer, not a refusal.
        return None, {}, False, response.status_code
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

# =========================================================================== #
# Database
# =========================================================================== #
# One page carries Autoplius' whole make/model catalogue: the "more models"
# pop-up embeds every make's models, with the site's own ids, as JSON.
CATALOGUE_URL = (f"{BASE}/skelbimai/paieska?category_id=2&filter=makes"
                 "&make_id=97&type=make_combo")


def band_url(base, lo, hi):
    parts = urlparse(base)
    q = parse_qs(parts.query)
    q["price_from"] = [str(lo)]
    q["price_to"] = [str(hi)]
    q.pop("page_nr", None)
    return urlunparse(parts._replace(query=urlencode(q, doseq=True)))


def id_url(make_id, model_id):
    return f"{DEFAULT_SEARCH}?make_id={make_id}&model_id={model_id}"


def parse_catalogue(html):
    """Every make and model from the catalogue page:
    [{"make", "model", "make_id", "model_id", "make_name", "model_name"}, ...].
    The site's "-Kita-" (other) entries are left out: they have no name to
    group by."""
    soup = BeautifulSoup(html or "", "html.parser")
    makes = {o.get("value"): o.get_text(strip=True)
             for o in soup.select("#make_id_list option") if o.get("value")}
    m = re.search(r"new FinnCart\(\s*'make_id'\s*,\s*", html or "")
    if not m or not makes:
        return []
    data, _ = json.JSONDecoder().raw_decode(html[m.end():])
    entries, seen = [], set()
    for make_id, models in data.items():
        make_name = makes.get(make_id)
        if not make_name or make_name.startswith("-") or not isinstance(models, dict):
            continue
        for model_key, model_name in models.items():
            model_id = int(model_key.lstrip("_"))
            if not model_name or model_name.strip("-").lower() == "kita" \
                    or model_name.startswith("-") or model_id in seen:
                continue
            seen.add(model_id)
            entries.append({
                "make": name_slug(make_name), "model": name_slug(model_name),
                "make_id": int(make_id), "model_id": model_id,
                "make_name": make_name, "model_name": model_name,
            })
    return entries


def fetch_catalogue(session, blocks):
    print(f"Fetching the make/model catalogue from {CATALOGUE_URL}")
    return parse_catalogue(fetch(session, CATALOGUE_URL, blocks=blocks))


def damaged_url(url):
    q = "&".join(f"has_damaged_id[{i}]={i}" for i in DAMAGE_IDS)
    return url + ("&" if "?" in url else "?") + q


def result_count(html):
    """The '(17)' total the site prints over a result list, or None."""
    m = re.search(r'class="result-count"[^>]*>\s*\(?\s*([\d\s ]+)\)', html or "")
    return clean_number(m.group(1)) if m else None

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


def parse_make_counts(html):
    """{make key: listings on the site} from the home page's make dropdown."""
    soup = BeautifulSoup(html or "", "html.parser")
    counts = {}
    for option in soup.select(".js-make .js-options .js-option"):
        name = option.get("data-title", "")
        if name and not name.startswith("-"):
            counts[name_slug(name)] = int(option.get("data-badge") or 0)
    return counts


# --------------------------------------------------------------------------- #
# hooks for the shared crawler and watcher
# --------------------------------------------------------------------------- #
newest_url = f"{DEFAULT_SEARCH}?order_by=3&order_direction=DESC"


def damaged_search_urls():
    """One search: ticking several defect types is an OR on Autoplius."""
    return [damaged_url(newest_url)]


def catalogue_url(entry):
    return id_url(entry["make_id"], entry["model_id"])


def fetch_make_counts(session):
    return parse_make_counts(fetch(session, f"{BASE}/", blocks=BlockDetector()))


def diagnose_targets():
    return [
        ("robots.txt", f"{BASE}/robots.txt"),
        ("main search page", DEFAULT_SEARCH),
        ("a model search (bmw/320)", id_url(97, 10941)),
        ("make/model catalogue", CATALOGUE_URL),
    ]
