"""
collectors/autogidas.py — everything specific to autogidas.lt (~27,000 cars).

How it differs from Autoplius, and what that means here:

  * Its model lists come only from /ajax and /select endpoints, which its
    robots.txt disallows. So searches go per MAKE (the make list with counts
    is embedded in every results page as `searchMakes`), and each car's model
    is read from its card title and matched against the shared catalogue:
    "Mercedes-Benz C 220 T CDI" lands under mercedes-benz/c220 like the same
    car on Autoplius. See MAKE_SEARCH and collectors.crawl.
  * Filters go by name: f_1[0]=BMW (make), f_215 / f_216 (price from / to),
    f_46=<defect> (one defect type per search), f_50=naujausi_asc (newest
    first: the site's "_desc" value is its OLDEST-first sort), page=N. Pages
    past the end wrap back to page 1.
  * Cards carry no body type; the ad URL does ("...-2006-m-vienaturis-...").

The hooks the shared crawler and watcher call are listed in
collectors/autoplius.py.
"""

import re
import sys
import unicodedata
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from collectors.common import (AD_GONE_STATUS, HEADERS, BlockDetector, clean_number,
                               fetch, name_slug, parse_age)

SOURCE = "autogidas"
PAGE_SIZE = 20
# One search per make; the model of each car comes from its title.
MAKE_SEARCH = True

BASE = "https://autogidas.lt"
SEARCH = f"{BASE}/skelbimai/automobiliai/"
AD_ID_RE = re.compile(r"/skelbimas/[^/?#]+-(\d+)\.html")

# Defect filter values (f_46). "Kiti smulkūs defektai" (other minor defects,
# ~1,200 cars) is left out, as Autoplius' filter leaves it out: scratches
# aren't damage in the sense the price model and verdicts mean.
DAMAGE_VALUES = ("Daužtas", "Degęs", "Pavarų dėžės defektas", "Pažeistas krušos",
                 "Skendęs", "Variklio defektas", "Kiti stambūs defektai")
NO_DEFECT = "Be defektų"

# Card / slug words -> the spellings Autoplius uses, so the price model sees
# one category per body type and fuel whichever site a car came from.
BODIES = {
    "universalas": "Universalas", "sedanas": "Sedanas", "hecbekas": "Hečbekas",
    "vienaturis": "Vienatūris", "visureigis--krosoveris": "Visureigis / Krosoveris",
    "visureigis": "Visureigis / Krosoveris", "coupe": "Kupė (Coupe)",
    "kupe": "Kupė (Coupe)", "kupe-coupe": "Kupė (Coupe)", "kabrioletas": "Kabrioletas",
    "pikapas": "Pikapas",
    "limuzinas": "Limuzinas", "komercinis-autosu-buda": "Komercinis",
    "komercinis-auto-su-buda": "Komercinis", "komercinis": "Komercinis",
    "visureigis-krosoveris": "Visureigis / Krosoveris",
}


def body_name(text):
    """A body type from a URL slug or an ad page ("Komercinis auto(su būda)")
    in Autoplius' spelling; None if it isn't one we know."""
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    key = name_slug(ascii_text)
    return BODIES.get(key) or BODIES.get(key.replace("--", "-"))
FUELS = {
    "dyzelinas": "Dyzelinas", "benzinas": "Benzinas", "elektra": "Elektra",
    "benzinas/dujos": "Benzinas/dujos", "benzinas/elektra": "Benzinas/elektra",
    "dyzelinas/elektra": "Dyzelinas/elektra", "dujos": "Dujos", "hibridas": "Hibridas",
}
GEARBOXES = {"automatinė": "Automatinė", "mechaninė": "Mechaninė"}


# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #
def _with(url, **params):
    parts = urlparse(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    for key, value in params.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = [str(value)]
    return urlunparse(parts._replace(query=urlencode(query, doseq=True)))


def page_url(url, page_nr):
    return _with(url, page=page_nr if page_nr > 1 else None)


def band_url(url, lo, hi):
    return _with(url, f_215=lo, f_216=hi, page=None)


def make_url(make_name):
    return f"{SEARCH}?f_1%5B0%5D={quote(make_name)}"


# "Naujausi viršuje" (newest first) is f_50=naujausi_asc on this site;
# naujausi_desc is "Seniausi viršuje" (oldest first). The watcher ran on _desc
# for a day and saw no new ad at all, so is_newest_first() now checks the
# page itself.
NEWEST_SORT = "naujausi_asc"
newest_url = f"{SEARCH}?f_50={NEWEST_SORT}"


def is_newest_first(html):
    """Whether a results page really is sorted newest first, read from its own
    sort box. None when the page has no sort box to tell by."""
    selected = re.search(r'<option value="\?f_50=([a-z_]+)"\s+selected', html or "")
    return selected.group(1) == NEWEST_SORT if selected else None


def damaged_search_urls():
    """One search per defect type: autogidas takes a single f_46 value."""
    return [f"{newest_url}&f_46={quote(value)}" for value in DAMAGE_VALUES]


def catalogue_url(entry):
    return make_url(entry["make_name"])


# --------------------------------------------------------------------------- #
# results pages
# --------------------------------------------------------------------------- #
def result_count(html):
    """"Rasta skelbimų: 26995" -> 26995, or None if this isn't a results page."""
    m = re.search(r'class="ads-total">\s*([\d\s\xa0]+)<', html or "")
    return clean_number(m.group(1)) if m else None


def _fuel(text):
    key = re.sub(r"\s*/\s*", "/", text.lower().strip())
    return FUELS.get(key)


def parse_card(article):
    """One car from a results-page <article>, in the same shape as Autoplius'."""
    link_el = article.select_one("a.item-link")
    if link_el is None:
        return None
    m = AD_ID_RE.search(link_el.get("href", ""))
    if not m:
        return None
    link = urljoin(BASE, link_el["href"].split("?")[0])
    title_el = article.select_one(".item-title")
    title = title_el.get_text(" ", strip=True) if title_el else ""

    price = None
    try:
        price = round(float(article.get("data-price") or ""))
    except ValueError:
        price_el = article.select_one(".item-price")
        price = clean_number(price_el.get_text(" ", strip=True)) if price_el else None
    if price is not None and price < 50:
        price = None

    year = mileage = engine = power = fuel = gearbox = location = None
    for el in article.select(".parameters .parameter-value"):
        text = el.get_text(" ", strip=True)
        low = text.lower()
        if re.fullmatch(r"(19|20)\d{2}\s*m\.?", low):
            year = int(low[:4])
        elif low.endswith("km") and re.search(r"\d", low):
            mileage = clean_number(low)
        elif re.search(r"\bkw\b|\d\s*l\b", low):
            em = re.search(r"(\d+[.,]\d)\s*l", low)
            pm = re.search(r"(\d{2,4})\s*kw", low)
            engine = em.group(1).replace(",", ".") if em else None
            power = int(pm.group(1)) if pm else None
        elif low in GEARBOXES:
            gearbox = GEARBOXES[low]
        elif _fuel(text):
            fuel = _fuel(text)
        elif "," in text and not re.search(r"\d", text):
            location = text.split(",")[0].strip()

    slug = urlparse(link).path.rsplit("/", 1)[-1]
    bm = re.search(r"-(?:19|20)\d{2}-m-(.+)-\d+\.html$", slug)
    body = body_name(bm.group(1)) if bm else None
    dealer = 1 if article.select_one(".business-logo, .company-name, .dealer-text") else 0

    return {
        "id": m.group(1), "title": title, "year": year or "",
        "price_eur": price if price is not None else "",
        "mileage_km": mileage if mileage is not None else "",
        "fuel": fuel or "", "engine_l": engine or "", "link": link,
        "power_kw": power, "gearbox": gearbox, "body": body,
        "location": location, "dealer": dealer,
    }


def scrape_page(html):
    """Yield one dict per car on a results page."""
    seen = set()
    for article in BeautifulSoup(html or "", "html.parser").select("article.list-item-new"):
        row = parse_card(article)
        if row and row["id"] not in seen and (row["price_eur"] != "" or row["year"] != ""):
            seen.add(row["id"])
            yield row


def card_ages(html):
    """{ad id: minutes since published} from the cards' "Prieš 18 min." badges.
    Each card also has a hidden "Žiūrėjote" (you viewed this) badge; skip it."""
    ages = {}
    for article in BeautifulSoup(html or "", "html.parser").select("article.list-item-new"):
        link = article.select_one("a.item-link")
        badge = article.select_one(".badge:not(.viewed-badge)")
        m = AD_ID_RE.search(link.get("href", "")) if link else None
        if m and badge:
            age = parse_age(badge.get("data-badge"))
            if age is not None:
                ages[m.group(1)] = age
    return ages


# --------------------------------------------------------------------------- #
# makes
# --------------------------------------------------------------------------- #
def parse_makes(html):
    """[(make name, listings)] from the `searchMakes` list every results page
    embeds; group headers and makes with no ads left out."""
    m = re.search(r"var searchMakes\s*=\s*(\[.*?\]);", html or "", re.S)
    if not m:
        return []
    import json
    makes = []
    for item in json.loads(m.group(1)):
        name, count = item.get("value"), item.get("count")
        if name and str(count).isdigit() and int(count) > 0:
            makes.append((name, int(count)))
    return makes


def fetch_catalogue(session, blocks):
    """One 'model' per make: the per-make search, with model "*" (all models).
    Ids are the make names; autogidas filters by name."""
    print(f"Fetching the make list from {SEARCH}")
    makes = parse_makes(fetch(session, SEARCH, blocks=blocks))
    return [{"make": name_slug(name), "model": "*", "make_id": None, "model_id": None,
             "make_name": name, "model_name": "*"} for name, _ in makes]


def parse_make_counts(html):
    return {name_slug(name): count for name, count in parse_makes(html)}


def fetch_make_counts(session):
    return parse_make_counts(fetch(session, SEARCH, blocks=BlockDetector()))


# --------------------------------------------------------------------------- #
# ad pages
# --------------------------------------------------------------------------- #
def parse_description(html):
    soup = BeautifulSoup(html or "", "html.parser")
    box = soup.select_one(".view-description .view-section-content")
    if box is None:
        return None
    text = box.get_text("\n", strip=True)
    return text or None


def parse_ad_details(html):
    """The fields the price model and verdicts use, from the ad's two tables:
    the icon row (Variklis "1.6 l., 66 kW", Pavarų dėžė ...) and the striped
    list (Kėbulo tipas, Defektai, Pirmosios registracijos šalis ...)."""
    soup = BeautifulSoup(html or "", "html.parser")
    table = {}
    for group in soup.select(".list-striped-item-group"):
        t = group.select_one(".list-striped-item-title")
        v = group.select_one(".list-striped-item-value")
        if t and v:
            table[t.get_text(" ", strip=True)] = v.get_text(" ", strip=True)
    for icon in soup.select("div > i"):
        b = icon.find_next_sibling("b")
        if b:
            table.setdefault(icon.get_text(" ", strip=True), b.get_text(" ", strip=True))
    if not table:
        return {}
    power = re.search(r"(\d{2,4})\s*kW", table.get("Variklis", ""))
    damage = table.get("Defektai") or None
    if damage in (NO_DEFECT, "Kiti smulkūs defektai"):
        damage = None
    gearbox = (table.get("Pavarų dėžė") or "").lower()
    return {
        "power_kw": int(power.group(1)) if power else None,
        "gearbox": GEARBOXES.get(gearbox),
        "body": body_name(table.get("Kėbulo tipas")),
        "damage_type": damage,
        "reg_country": table.get("Pirmosios registracijos šalis") or None,
    }


def fetch_ad_details(session, url, blocks=None):
    """Fetch one public ad page; return (description, details, checked, HTTP status)."""
    parts = urlparse(url)
    if parts.scheme != "https" or parts.hostname not in {"autogidas.lt", "www.autogidas.lt"}:
        raise ValueError(f"Refusing a non-autogidas listing URL: {url}")
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
    details = parse_ad_details(body)
    if not details:                       # not an ad page: a challenge, or an error page
        print(f"  ! {url} is not an ad page", file=sys.stderr)
        if blocks is not None:
            blocks.record_refusal(response.status_code, body)
        return None, {}, False, response.status_code
    if blocks is not None:
        blocks.record_success()
    return parse_description(body), details, True, response.status_code


def diagnose_targets():
    return [
        ("robots.txt", f"{BASE}/robots.txt"),
        ("results page", SEARCH),
        ("a make search (BMW)", make_url("BMW")),
        ("newest first", newest_url),
    ]
