"""
collectors/verdicts.py — what a cheap car really is, and what you said it is.

Every car priced under what the model expects for it gets one verdict:

    not a real price   under 30% of expected, or under 50% and so far below
                       its group that the group's own percentiles exclude it:
                       a lease payment, a deposit, a typo, a parts car. Also
                       an ad whose own text says so (an auction bid, a price
                       without VAT, a down payment), and in the watcher a car
                       under half of what enough similar cars ask
    damaged            a defect declared on the site (its damage filter or the
                       ad's "Defektai" row)
    problem            problem words in the seller's own text: not running,
                       no TA, export, parts, lease transfer, no documents,
                       engine trouble; or a US import / UK (right-hand drive)
                       car, by its text or first-registration country
    deal               under the low end of its range (p10), ad read, nothing
                       found
    unchecked          under the low end, but the ad hasn't been read yet
    (none)             a little under expected but not under the low end: not
                       a candidate

Your labels (the `labels` table, set from the report or the dashboard) sit on
top: "deal" (a real deal), "damaged" (crashed / damaged) and "fake" (not a
real price). The report shows both, and measures how often they agree.

The report and the watcher both use classify(), so a car gets the same
verdict wherever it's shown.
"""

import re
from datetime import datetime, timezone

VERDICTS = ("deal", "unchecked", "damaged", "problem", "not a real price")
LABELS = {"deal": "real deal", "damaged": "damaged", "fake": "fake price"}
# Which verdicts each label agrees with: "damaged" covers problem cars too,
# since "not running" or "no TA" is the same answer to "is this a deal?".
AGREES = {"deal": {"deal"}, "damaged": {"damaged", "problem"},
          "fake": {"not a real price"}}

# Under this share of the expected price a listing is not a real asking price.
IMPLAUSIBLE_SHARE = 0.3
# A car its group's percentiles exclude is "not a real price" only under this
# share of expected (see classify).
OUTLIER_CONFIRM_SHARE = 0.5


# --------------------------------------------------------------------------- #
# Problem words in the seller's own text
# --------------------------------------------------------------------------- #
# Phrases that DENY a problem are removed first, so "be defektų", "nedaužtas"
# or "jokių gedimų" never count as hits. \b keeps "nedaužtas" from matching
# "daužt" too, but the explicit list is what makes the intent readable.
NEGATIONS_RE = re.compile(
    r"\b(be|nėra|nera|jokių|jokiu|neturi|nebuvo|neturėjo)\s+(jokių\s+|jokiu\s+)?"
    r"(defekt|gedim|daužt|dauzt|pažeidim|pazeidim|avarij|autoįvyk|autoivyk|įvyk|ivyk"
    r"|apgadin)\w*"
    r"|\bned[aа]u[žz]\w*|\bnebuvo\s+daužt\w*|\bnot\s+damaged\b|\bno\s+damage\b"
    # A dealer quoting a second, VAT-free price for buyers abroad is not
    # selling "for export only": "Kaina eksportui / be PVM 9 300 Eur".
    r"|kaina\s+eksportui|eksporto\s+kaina|\bexport\s+price\b|price\s+for\s+export",
    re.I)

# Every pattern below that isn't a plain dictionary word came from a real ad
# that had passed as a "deal": "Po autoivykio", "Brauktas sonas", "Tepalas
# aušinimo sistemoje", "dokumentu nebus", "PARDUODAMA DALIM", "V5 dokumentai",
# "Dabartinis statymas - 8.900€", "12.100€+PVM".
FLAG_PATTERNS = {
    "damaged": r"\bdaužt\w*|\bdauzt\w*|\bdaužyt\w*|\bdau[žz]im\w*|po\s+avarijos|\bavarin\w*"
               r"|po\s+(\w+\s+)?(eismo\s+)?(auto)?[įi]vykio"
               r"|\bbraukt\w*\s+[šs]on\w*|\bnubraukt\w*|\bapgadin\w*|\bdefekt\w*"
               r"|\bgedim\w*|\bsugedu\w*|\bsugedęs|\bremontui\b|reikia\s+remonto"
               r"|reikalauja\s+remonto|\bбит(ый|ая|ое)\b|\bdamaged\b|\baccident\b",
    "engine trouble": r"tepal\w*\s+au[šs]inimo|au[šs]inimo\s+skyst\w*\s+tepal\w*"
                      r"|[ėe]da\s+tepal\w*|variklis\s+stuksi|stuksi\s+variklis"
                      r"|reikia\s+keisti\s+varikl[įi]\b|variklio\s+remont\w*",
    "not running": r"neužsiveda|neuzsiveda|nevažiuoja|nevaziuoja|nesiveda|не\s+заводится"
                   r"|\bnot\s+running\b|does\s+not\s+start|neveikia\s+variklis",
    "no inspection": r"\bbe\s+t\.?\s?a\.?(?=\W|$)|ta\s+negalioja|nėra\s+ta\b|be\s+techninės",
    "export": r"\beksportui\b|\bexport\b|\bэкспорт\w*",
    "parts": r"\bdalim(is)?\b|\bardom\w*|\bna\s+zapchasti\b|на\s+запчасти|\bfor\s+parts\b",
    "lease transfer": r"lizing\w*\s+(perėmim|perimim|perleid)\w*|perimti\s+lizing\w*"
                      r"|perimamas\s+lizing\w*",
    "no documents": r"be\s+dokument\w*|dokument\w*\s+nebus|n[ėe]ra\s+dokument\w*"
                    r"|be\s+registracijos|be\s+numeri\w*|be\s+perra[šs]ymo"
                    r"|išregistruot\w*|neregistruot\w*",
    # A UK car: right-hand drive, worth far less here than its left-hand twin.
    # ("V5" alone is also a Volkswagen engine, so it needs "dokumentai" with it.)
    "UK registration": r"\bv5c?\s+dokument\w*|\bpilnas\s+v5c?\b|\bv5c\b"
                       r"|\bmot\s+(yra|iki|galioja)\b|angli[šs]k\w*\s+(numer|registr|dokument)\w*"
                       r"|de[šs]in\w*\s+vair\w*|vairas\s+de[šs]in\w*|\bright[\s-]hand\s+drive\b",
    # --- the number in the ad is not what the car costs -------------------- #
    # An auction lot: the "price" is the current bid.
    "auction bid": r"dabartinis\s+statymas|aukciono\s+nuoroda|aukciono\s+b[ūu]du"
                   r"|aukciono\s+prek[ėe]|bus\s+parduodamas\s+(\w+\s+)?aukcione"
                   r"|copart\.\w+/lot|\.auction/",
    # "12.100€+PVM", "Kaina plius PVM": 21% comes on top. ("Kaina su PVM ...,
    # be PVM ..." and "Kaina eksportui / be PVM" give the full price too, and
    # are left alone.)
    "price without VAT": r"\+\s*pvm\b|plius\s+pvm\b|kaina\s+be\s+pvm\b|\+\s*vat\b",
    # Leasing is offered in every tenth ad next to a real price; only an ad
    # saying the listed number itself is a down payment or an instalment counts.
    "down payment": r"(nurodyta\s+)?kaina\s*(yra|[-–:])?\s*(tik\s+)?"
                    r"(pradin\w+\s+[įi](na[šs]|mok)\w*|pirm\w+\s+[įi]mok\w*"
                    r"|m[ėe]nesi\w*\s+[įi]mok\w*|avans\w*)",
}
FLAG_RES = {name: re.compile(p, re.I) for name, p in FLAG_PATTERNS.items()}
# Flags that mean "not a real price" rather than "a car with a problem".
FAKE_PRICE_FLAGS = {"auction bid", "price without VAT", "down payment"}

# First-registration countries that explain a low price by themselves.
# US and Canadian cars are mostly rebuilt insurance write-offs; UK and Irish
# ones have the wheel on the right.
COUNTRY_FLAGS = {
    "jav": "US import", "kanada": "US import", "usa": "US import",
    "anglija": "UK registration", "didžioji britanija": "UK registration",
    "jungtinė karalystė": "UK registration", "airija": "UK registration",
}

# With enough similar cars to compare with, a price under this share of their
# median is not a discount (a 2021 BMW 520 for 8,900 against 28,000). Only for
# cars worth real money: an old car at half its neighbours' 1,200 is just tired.
TOO_CHEAP_SHARE = 0.5
TOO_CHEAP_MIN_MEDIAN = 5000
TOO_CHEAP_FLAG = "under half of similar cars"


def text_flags(*texts):
    """Problem keywords found in a title / description. Edit FLAG_PATTERNS freely:
    flags are recomputed on every run, so nothing has to be re-fetched."""
    text = NEGATIONS_RE.sub(" ", " ".join(t for t in texts if t))
    return [name for name, rx in FLAG_RES.items() if rx.search(text)]


def country_flags(reg_country):
    """The flag a first-registration country carries ("JAV" -> US import), if any."""
    flag = COUNTRY_FLAGS.get((reg_country or "").strip().lower())
    return [flag] if flag else []


def too_cheap(price, similar_n, similar_median, min_n=5):
    """Under half of what enough similar cars ask (see TOO_CHEAP_SHARE)."""
    return bool(similar_n and similar_median and similar_n >= min_n
                and similar_median >= TOO_CHEAP_MIN_MEDIAN
                and price < TOO_CHEAP_SHARE * similar_median)


def classify(price, lo, mid, declared=False, flags=(), read=False, outlier=False):
    """
    The verdict for one car (see the module docstring), or None for a car that
    isn't a candidate. `declared`: a defect declared on the site; `flags`:
    problem words found; `read`: its ad page has been read; `outlier`: its
    group's percentiles excluded its price.
    """
    if not price or not mid or price >= mid:
        return None
    # A group outlier alone isn't proof: groups (generation + year band) mix
    # trims and engines, so a fair price can sit outside its group's spread (a
    # 2025 Skoda Scala at 92% of expected was flagged that way). It only counts
    # when the price is also far below what the model expects for this car.
    if price < IMPLAUSIBLE_SHARE * mid or (outlier and price < OUTLIER_CONFIRM_SHARE * mid):
        return "not a real price"
    # The seller's own text says the number isn't the price: an auction bid,
    # a price without VAT, a down payment.
    if any(flag in FAKE_PRICE_FLAGS or flag == TOO_CHEAP_FLAG for flag in flags):
        return "not a real price"
    if declared:
        return "damaged"
    if flags:
        return "problem"
    if lo and price < lo:
        return "deal" if read else "unchecked"
    return None


def label_agrees(label, verdict):
    return verdict in AGREES.get(label, ())


# --------------------------------------------------------------------------- #
# labels
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS labels (
    source     TEXT NOT NULL,
    id         TEXT NOT NULL,
    label      TEXT NOT NULL,      -- deal / damaged / fake
    labeled_at TEXT,
    PRIMARY KEY (source, id)
);
"""


def set_label(conn, source, ad_id, label):
    """Save your label for one car; an empty label removes it."""
    conn.executescript(SCHEMA)
    if label:
        if label not in LABELS:
            raise ValueError(f"Unknown label '{label}'")
        conn.execute("INSERT OR REPLACE INTO labels VALUES (?,?,?,?)",
                     (source, ad_id, label,
                      datetime.now(timezone.utc).isoformat(timespec="seconds")))
    else:
        conn.execute("DELETE FROM labels WHERE source = ? AND id = ?", (source, ad_id))
    conn.commit()


def load_labels(conn):
    """{(source, id): label}"""
    conn.executescript(SCHEMA)
    return {(s, i): label for s, i, label in conn.execute("SELECT source, id, label FROM labels")}
