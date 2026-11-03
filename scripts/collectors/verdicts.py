"""
collectors/verdicts.py — what a cheap car really is, and what you said it is.

Every car priced under what the model expects for it gets one verdict:

    not a real price   under 30% of expected, or so far below its group that
                       the group's own percentiles exclude it: a lease
                       payment, a deposit, a typo, a parts car
    damaged            a defect declared on the site (its damage filter or the
                       ad's "Defektai" row)
    problem            problem words in the seller's own text: not running,
                       no TA, export, parts, lease transfer, no documents
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


# --------------------------------------------------------------------------- #
# Problem words in the seller's own text
# --------------------------------------------------------------------------- #
# Phrases that DENY a problem are removed first, so "be defektų", "nedaužtas"
# or "jokių gedimų" never count as hits. \b keeps "nedaužtas" from matching
# "daužt" too, but the explicit list is what makes the intent readable.
NEGATIONS_RE = re.compile(
    r"\b(be|nėra|nera|jokių|jokiu|neturi|nebuvo|neturėjo)\s+(jokių\s+|jokiu\s+)?"
    r"(defekt|gedim|daužt|dauzt|pažeidim|pazeidim|avarij)\w*"
    r"|\bned[aа]u[žz]\w*|\bnebuvo\s+daužt\w*|\bnot\s+damaged\b|\bno\s+damage\b",
    re.I)

FLAG_PATTERNS = {
    "damaged": r"\bdaužt\w*|\bdauzt\w*|\bdaužyt\w*|po\s+avarijos|\bavarin\w*|\bdefekt\w*"
               r"|\bgedim\w*|\bsugedu\w*|\bsugedęs|\bremontui\b|reikia\s+remonto"
               r"|reikalauja\s+remonto|\bбит(ый|ая|ое)\b|\bdamaged\b|\baccident\b",
    "not running": r"neužsiveda|neuzsiveda|nevažiuoja|nevaziuoja|nesiveda|не\s+заводится"
                   r"|\bnot\s+running\b|does\s+not\s+start|neveikia\s+variklis",
    "no inspection": r"\bbe\s+t\.?\s?a\.?(?=\W|$)|ta\s+negalioja|nėra\s+ta\b|be\s+techninės",
    "export": r"\beksportui\b|\bexport\b|\bэкспорт\w*",
    "parts": r"\bdalimis\b|\bardom\w*|\bna\s+zapchasti\b|на\s+запчасти|\bfor\s+parts\b",
    "lease transfer": r"lizing\w*\s+(perėmim|perimim|perleid)\w*|perimti\s+lizing\w*"
                      r"|perimamas\s+lizing\w*",
    "no documents": r"be\s+dokument\w*|be\s+registracijos|išregistruot\w*|neregistruot\w*",
}
FLAG_RES = {name: re.compile(p, re.I) for name, p in FLAG_PATTERNS.items()}


def text_flags(*texts):
    """Problem keywords found in a title / description. Edit FLAG_PATTERNS freely:
    flags are recomputed on every run, so nothing has to be re-fetched."""
    text = NEGATIONS_RE.sub(" ", " ".join(t for t in texts if t))
    return [name for name, rx in FLAG_RES.items() if rx.search(text)]


def classify(price, lo, mid, declared=False, flags=(), read=False, outlier=False):
    """
    The verdict for one car (see the module docstring), or None for a car that
    isn't a candidate. `declared`: a defect declared on the site; `flags`:
    problem words found; `read`: its ad page has been read; `outlier`: its
    group's percentiles excluded its price.
    """
    if not price or not mid or price >= mid:
        return None
    if price < IMPLAUSIBLE_SHARE * mid or outlier:
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
