"""The gap-free watcher: age badges, model matching, the page walk, verdicts."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from collectors import autoplius as db
from collectors import watch as w
from collectors.common import parse_age


# --------------------------------------------------------------------------- #
# age badges
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, minutes", [
    ("Mažiau nei prieš minutę", 0),
    ("Prieš 7 min.", 7),
    ("Prieš 1 val.", 60),
    ("Prieš 1 val. 15 min.", 75),
    ("Prieš 2 d.", 2880),
    ("", None),
    ("TOP", None),
])
def test_parse_age(text, minutes):
    assert parse_age(text) == minutes


def test_card_ages_on_newest_page(page):
    ages = db.card_ages(page("autoplius/results_newest.html"))
    assert len(ages) == 20
    values = list(ages.values())
    assert values[0] == 0 and values[-1] == 12
    assert values == sorted(values)          # newest-first really is by age


# --------------------------------------------------------------------------- #
# make / model from titles
# --------------------------------------------------------------------------- #
PAIRS = [("bmw", "320"), ("bmw", "120"), ("bmw", "320-gran-turismo"),
         ("mercedes-benz", "glk320"), ("land-rover", "range-rover"),
         ("land-rover", "range-rover-sport"), ("volkswagen", "caddy"),
         ("volkswagen", "caddy-maxi"), ("lexus", "ct-200h")]


@pytest.mark.parametrize("title, expected", [
    ("BMW 320", ("bmw", "320", True)),
    ("BMW 320 Gran Turismo", ("bmw", "320-gran-turismo", True)),
    ("Mercedes-Benz GLK320", ("mercedes-benz", "glk320", True)),
    ("Land Rover Range Rover Sport", ("land-rover", "range-rover-sport", True)),
    ("Volkswagen Caddy Maxi", ("volkswagen", "caddy-maxi", True)),
    ("Lexus CT 200h", ("lexus", "ct-200h", True)),
    # a number can't run on: "120" must not claim a "1200"
    ("BMW 1200", ("bmw", "1200", False)),
    # models the database doesn't know keep their full name
    ("Tesla Model 3", ("tesla", "model-3", False)),
    ("Volkswagen ID.4", ("volkswagen", "id-4", False)),
    ("DS Automobiles DS7 Crossback", ("ds-automobiles", "ds7-crossback", False)),
    ("Alfa Romeo Tonale", ("alfa-romeo", "tonale", False)),
])
def test_model_resolver(title, expected):
    assert w.ModelResolver(PAIRS).resolve(title) == expected


# --------------------------------------------------------------------------- #
# the walk
# --------------------------------------------------------------------------- #
def results_page(ids):
    """A minimal newest-first results page holding these ad ids."""
    cards = "".join(
        f'<a class="announcement-item" href="/skelbimai/bmw-320-{i}.html">'
        f'<div class="announcement-title">BMW 320</div>'
        f'<div class="announcement-badges"><div class="announcement-badge badge-new">'
        f'Prieš {n} min.</div></div><span>2015</span><strong>9 000 €</strong></a>'
        for n, i in enumerate(ids))
    return f"<html><body>{cards}</body></html>"


def run_walk(pages, known, margin=1, cap=50):
    calls = []

    def get_page(n):
        calls.append(n)
        return results_page(pages[n - 1]) if n <= len(pages) else ""
    return w.walk(db, get_page, lambda i: i in known, margin, cap), calls


def test_walk_stops_after_a_known_page_plus_margin():
    pages = [["10", "11"], ["12", "13"], ["1", "2"], ["3", "4"], ["5", "6"]]
    result, calls = run_walk(pages, known={"1", "2", "3", "4", "5", "6"})
    assert list(result["rows"]) == ["10", "11", "12", "13"]
    assert calls == [1, 2, 3, 4]              # page 3 known, page 4 is the margin
    assert result["reached_known"] and not result["hit_cap"]


def test_quiet_period_costs_two_pages():
    result, calls = run_walk([["1", "2"], ["3", "4"], ["5"]], known={"1", "2", "3", "4", "5"})
    assert calls == [1, 2] and not result["rows"]


def test_one_known_promoted_ad_does_not_stop_the_walk():
    # "1" is an old raised ad mixed in at the top; the pages are otherwise new
    pages = [["1", "20", "21"], ["22", "23"], ["2", "3"], ["4"]]
    result, _ = run_walk(pages, known={"1", "2", "3", "4"})
    assert list(result["rows"]) == ["20", "21", "22", "23"]


def test_ads_shifting_between_pages_are_counted_once():
    pages = [["30", "31"], ["31", "32"], ["1"], ["2"]]   # "31" pushed onto page 2
    result, _ = run_walk(pages, known={"1", "2"})
    assert list(result["rows"]) == ["30", "31", "32"]


def test_walk_hits_the_cap_when_everything_is_new():
    pages = [[str(100 + 2 * i), str(101 + 2 * i)] for i in range(10)]
    result, calls = run_walk(pages, known=set(), cap=4)
    assert result["hit_cap"] and calls == [1, 2, 3, 4] and len(result["rows"]) == 8


def test_failed_first_page():
    result, _ = run_walk([], known=set())
    assert result["failed"] and result["pages"] == 0 and not result["rows"]


def test_walk_keeps_card_ages():
    result, _ = run_walk([["10", "11"], ["1"], ["2"]], known={"1", "2"})
    assert result["ages"]["10"] == 0 and result["ages"]["11"] == 1


# --------------------------------------------------------------------------- #
# verdicts
# --------------------------------------------------------------------------- #
def test_verdicts():
    assert w.verdict(500, 4000, 6000)[0] == "not a real price"
    assert w.verdict(3500, 4000, 6000, {"damage_type": "Daužtas"}, read=True) == \
        ("damaged", ["Daužtas"])
    assert w.verdict(3500, 4000, 6000, {}, "BMW 320", "Variklis neužsiveda", read=True) == \
        ("problem", ["not running"])
    assert w.verdict(3500, 4000, 6000, {}, "BMW 320", "Tvarkingas", read=True)[0] == "deal"
    assert w.verdict(3500, 4000, 6000)[0] == "unchecked"


# --------------------------------------------------------------------------- #
# health, as the dashboard reads it
# --------------------------------------------------------------------------- #
def test_health_reports_failures_and_staleness():
    con = sqlite3.connect(":memory:")
    con.executescript(w.SCHEMA)
    assert w.health(con, "autoplius")["last"] is None
    now = datetime.now(timezone.utc)
    rows = [(now - timedelta(minutes=40), "ok", 2), (now - timedelta(minutes=10), "failed", 0),
            (now - timedelta(minutes=5), "failed", 0)]
    for started, status, pages in rows:
        con.execute("""INSERT INTO watch_scans (source, started_at, status, pages, new,
                       reached_known, hit_cap) VALUES ('autoplius', ?, ?, ?, 3, 1, 0)""",
                    (started.isoformat(timespec="seconds"), status, pages))
    h = w.health(con, "autoplius")
    assert h["failures"] == 2 and h["stale"] and h["new_24h"] == 9


def test_health_without_tables_is_none():
    assert w.health(sqlite3.connect(":memory:"), "autoplius") is None
