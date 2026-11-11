"""The watchlist: which new deals get a star and a Telegram message."""

import sqlite3

import pytest

from collectors import alerts, watchlist
from collectors import watch as w

MAKES = {"bmw", "audi", "mercedes-benz", "volkswagen"}
PAIRS = {("bmw", "320"), ("bmw", "520"), ("audi", "a4"), ("mercedes-benz", "c220"),
         ("volkswagen", "passat")}


def wl(**kw):
    return {**watchlist.EMPTY, **kw}


# --------------------------------------------------------------------------- #
# what was typed
# --------------------------------------------------------------------------- #
def test_makes_and_models_are_read_the_way_people_type_them():
    assert watchlist.parse_makes("BMW, audi a4; Mercedes-Benz C220\nvolkswagen/passat, bmw",
                                 MAKES, PAIRS) == \
        ["bmw", "audi/a4", "mercedes-benz/c220", "volkswagen/passat"]
    assert watchlist.parse_makes("", MAKES, PAIRS) == []
    # no catalogue yet (an empty database): nothing to check against
    assert watchlist.parse_makes("Tesla Model 3") == ["tesla-model-3"]


@pytest.mark.parametrize("text, message", [
    ("bwm", "Unknown make 'bwm'"),
    ("audi a44", "Unknown model 'audi a44'"),
    ("bmw/999", "Unknown model 'bmw/999'"),
])
def test_a_typo_is_refused_not_saved(text, message):
    with pytest.raises(ValueError, match=message):
        watchlist.parse_makes(text, MAKES, PAIRS)


def test_parse_checks_the_numbers():
    saved = watchlist.parse({"makes": "bmw", "price_max": 15000, "year_min": 2012,
                             "min_discount": 20, "include_maybe": True}, MAKES, PAIRS)
    assert saved == wl(makes=["bmw"], price_max=15000, year_min=2012, min_discount=20,
                       include_maybe=True)
    with pytest.raises(ValueError, match="Price from is higher"):
        watchlist.parse({"price_min": 9000, "price_max": 5000})
    with pytest.raises(ValueError, match="Discount must be a number"):
        watchlist.parse({"min_discount": 150})
    with pytest.raises(ValueError, match="Year from"):
        watchlist.parse({"year_min": "new"})


# --------------------------------------------------------------------------- #
# what fits
# --------------------------------------------------------------------------- #
def test_matching():
    mine = wl(makes=["bmw", "audi/a4"], price_min=3000, price_max=15000, year_min=2012,
              km_max=250_000, min_discount=15)
    fits = dict(price=9000, year=2015, km=180_000, discount_=0.22)
    assert watchlist.matches(mine, "bmw", "520", **fits)
    assert watchlist.matches(mine, "audi", "a4", **fits)
    assert not watchlist.matches(mine, "audi", "a6", **fits)              # another model
    assert not watchlist.matches(mine, "volvo", "v60", **fits)            # another make
    assert not watchlist.matches(mine, "bmw", "520", **{**fits, "price": 16000})
    assert not watchlist.matches(mine, "bmw", "520", **{**fits, "price": 2500})
    assert not watchlist.matches(mine, "bmw", "520", **{**fits, "year": 2010})
    assert not watchlist.matches(mine, "bmw", "520", **{**fits, "km": 300_000})
    assert not watchlist.matches(mine, "bmw", "520", **{**fits, "discount_": 0.10})
    assert watchlist.matches(mine, "bmw", "520", **{**fits, "km": None})  # mileage not shown
    # an empty watchlist marks nothing
    assert not watchlist.matches(wl(), "bmw", "520", **fits)


def test_discount_is_against_similar_cars_when_there_are_enough():
    assert watchlist.discount(8000, 12000, 20, 10000) == pytest.approx(0.20)
    assert watchlist.discount(8000, 12000, 2, 10000) == pytest.approx(1 / 3)   # too few: model
    assert watchlist.discount(8000, None) is None


def test_alerts_follow_the_watchlist():
    # no watchlist: every confirmed deal, as before
    assert watchlist.should_alert(wl(), "deal", None)
    assert not watchlist.should_alert(wl(), "maybe", None)
    mine = wl(makes=["bmw"])
    assert watchlist.should_alert(mine, "deal", True)
    assert not watchlist.should_alert(mine, "deal", False)        # a deal, but not one of yours
    assert not watchlist.should_alert(mine, "maybe", True)
    assert watchlist.should_alert(wl(makes=["bmw"], include_maybe=True), "maybe", True)
    assert not watchlist.should_alert(mine, "damaged", True)


def test_a_maybe_is_announced_as_a_possible_deal():
    text = alerts.deal_message("autoplius", "BMW 520", 2018, 9000, 14000, 11000, "https://x/1",
                               similar=(3, 0, 13000), verdict="maybe")
    assert text.startswith("Possible deal on autoplius: BMW 520 2018")
    assert "Cheaper than 3 of 3 similar cars" in text


# --------------------------------------------------------------------------- #
# kept in the database
# --------------------------------------------------------------------------- #
def test_saved_loaded_and_applied_to_the_deals_already_listed():
    con = sqlite3.connect(":memory:")
    w.ensure_tables(con)
    assert watchlist.load(con) == watchlist.EMPTY              # no meta table yet
    for ad_id, make, model, price in (("1", "bmw", "520", 9000), ("2", "audi", "a6", 7000),
                                      ("3", "bmw", "320", 30000)):
        con.execute("""INSERT INTO new_deals (source, id, make, model, price, year, mileage_km,
                           mid, verdict) VALUES ('autoplius', ?, ?, ?, ?, 2016, 150000, ?, 'deal')""",
                    (ad_id, make, model, price, price * 1.4))
    mine = wl(makes=["bmw"], price_max=15000)
    watchlist.save(con, mine)
    assert watchlist.load(con) == mine
    assert watchlist.reflag(con, mine) == 1
    assert dict(con.execute("SELECT id, watch FROM new_deals")) == {"1": 1, "2": 0, "3": 0}
    # cleared: nothing is marked either way
    assert watchlist.reflag(con, wl()) == 0
    assert set(con.execute("SELECT watch FROM new_deals")) == {(None,)}
    assert watchlist.describe(mine) == "bmw; €0–15,000"
    assert watchlist.describe(wl()).startswith("no watchlist")
