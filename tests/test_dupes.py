"""The same car on two sites: scoring, pairing, merging, and the watcher's lookup."""

import sqlite3

import pytest

import autoplius_stats as st
from collectors import watch as w
from collectors.dupes import (find_duplicates, match_score, same_car_elsewhere,
                              save_duplicates)
from collectors.store import Store


def car(source, ad_id, **kw):
    base = {"source": source, "id": ad_id, "make": "bmw", "model": "320", "year": 2015,
            "mileage_km": 187_654, "price_eur": 12_900, "fuel": "Dyzelinas",
            "engine_l": "2.0", "power_kw": 140, "gearbox": "Automatinė",
            "location": "Kaunas", "dealer": 1, "link": f"https://{source}/{ad_id}",
            "gone": False}
    return {**base, **kw}


# --------------------------------------------------------------------------- #
# score
# --------------------------------------------------------------------------- #
def test_the_same_car_scores_high():
    assert match_score(car("autoplius", "1"), car("autogidas", "a")) == 9.0


@pytest.mark.parametrize("change", [
    {"fuel": "Benzinas"}, {"gearbox": "Mechaninė"}, {"engine_l": "3.0"},
    {"power_kw": 135}, {"year": 2016}, {"model": "330"}, {"mileage_km": 160_000},
])
def test_any_contradiction_rules_a_pair_out(change):
    assert match_score(car("autoplius", "1"), car("autogidas", "a", **change)) == 0.0


def test_unknown_fields_neither_help_nor_hurt():
    a = car("autoplius", "1", power_kw=None, location=None, gearbox=None)
    assert match_score(a, car("autogidas", "a")) == 6.0          # km 3 + engine + price + dealer


def test_round_mileage_counts_less_and_another_town_counts_against():
    a = car("autoplius", "1", mileage_km=230_000, location="Vilnius", price_eur=450)
    b = car("autogidas", "a", mileage_km=230_000, location="Prienai", price_eur=1360)
    assert match_score(a, b) < 0                                  # two different Alfa 159s


# --------------------------------------------------------------------------- #
# pairing
# --------------------------------------------------------------------------- #
def test_cheaper_listing_is_shown_ties_go_to_autoplius():
    pairs = find_duplicates([car("autoplius", "1", price_eur=13_000),
                             car("autogidas", "a", price_eur=12_900)])
    assert [(s["id"], o["id"]) for s, o, _ in pairs] == [("a", "1")]
    pairs = find_duplicates([car("autogidas", "a"), car("autoplius", "1")])
    assert [(s["source"], o["source"]) for s, o, _ in pairs] == [("autoplius", "autogidas")]


def test_each_listing_joins_at_most_one_pair():
    cars = [car("autoplius", "1"), car("autoplius", "2", location="Vilnius"),
            car("autogidas", "a")]
    pairs = find_duplicates(cars)
    assert len(pairs) == 1 and {pairs[0][0]["id"], pairs[0][1]["id"]} == {"1", "a"}


def test_same_site_listings_are_never_paired():
    assert find_duplicates([car("autoplius", "1"), car("autoplius", "2")]) == []


def test_mileage_that_differs_needs_more_evidence():
    a = car("autoplius", "1", mileage_km=322_000, location="Vilnius", price_eur=6000,
            dealer=0)
    b = car("autogidas", "a", mileage_km=327_608, location="Vilnius", price_eur=8300,
            dealer=0)
    assert 4 <= match_score(a, b) < 5 and find_duplicates([a, b]) == []
    b["price_eur"] = 6100                                         # now the price agrees too
    assert len(find_duplicates([a, b])) == 1


# --------------------------------------------------------------------------- #
# report merge
# --------------------------------------------------------------------------- #
def test_merge_keeps_one_row_per_car_and_saves_pairs(tmp_path):
    db = tmp_path / "t.db"
    Store(db).close()
    cars = [car("autoplius", "1", price_eur=13_500), car("autogidas", "a"),
            car("autoplius", "2", model="x5", gone=False),
            car("autogidas", "b", model="x5", gone=True)]           # gone: not matched
    kept, n = st.merge_duplicates(cars, db)
    assert n == 1 and [c["id"] for c in kept] == ["a", "2", "b"]
    assert [o["id"] for o in kept[0]["also"]] == ["1"]
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT source, id, dup_source, dup_id FROM duplicates").fetchall() \
            == [("autogidas", "a", "autoplius", "1")]


def test_save_duplicates_replaces_old_pairs(tmp_path):
    con = sqlite3.connect(tmp_path / "t.db")
    save_duplicates(con, [(car("autoplius", "1"), car("autogidas", "a"), 9.0)])
    save_duplicates(con, [])
    assert con.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# watcher
# --------------------------------------------------------------------------- #
def listing(ad_id, **kw):
    r = {"id": ad_id, "title": "BMW 320", "year": 2015, "price_eur": 12_900,
         "mileage_km": 187_654, "fuel": "Dyzelinas", "engine_l": "2.0", "power_kw": 140,
         "gearbox": "Automatinė", "location": "Kaunas", "dealer": 1, "link": f"https://x/{ad_id}"}
    return {**r, **kw}


def test_watcher_finds_the_same_car_on_the_other_site(tmp_path):
    store = Store(tmp_path / "t.db")
    store.site("autoplius").upsert_many([listing("1"), listing("2", mileage_km=90_000)],
                                        "bmw", "320")
    new = car("autogidas", "a")
    found = same_car_elsewhere(store.conn, "autogidas", new)
    assert found["source"] == "autoplius" and found["id"] == "1"
    assert same_car_elsewhere(store.conn, "autoplius", car("autoplius", "9")) is None
    store.close()


def test_new_deals_carry_the_other_listing(tmp_path):
    store = Store(tmp_path / "t.db")
    watch = w.WatchStore(store, "autogidas")
    watch.record_deal(listing("a"), "bmw", "320", w.now_utc(), 14000, 16000, 19000, "deal",
                      [], {"source": "autoplius", "price_eur": 13_100, "link": "https://p/1"})
    deal = w.health(store.conn, "autogidas")["deals"][0]
    assert deal["also"] == "autoplius|13100|https://p/1" and deal["source"] == "autogidas"
    store.close()


def test_old_new_deals_table_gains_the_column(tmp_path):
    con = sqlite3.connect(tmp_path / "t.db")
    con.execute("""CREATE TABLE new_deals (source TEXT, id TEXT, found_at TEXT, make TEXT,
                   model TEXT, title TEXT, year INTEGER, mileage_km INTEGER, price INTEGER,
                   lo INTEGER, mid INTEGER, hi INTEGER, verdict TEXT, flags TEXT, link TEXT,
                   PRIMARY KEY (source, id))""")
    con.commit()
    con.close()
    store = Store(tmp_path / "t.db")
    w.WatchStore(store, "autoplius")
    assert "also" in {r[1] for r in store.conn.execute("PRAGMA table_info(new_deals)")}
    store.close()


# --------------------------------------------------------------------------- #
# saved price model compatibility
# --------------------------------------------------------------------------- #
def test_a_model_saved_before_source_existed_keeps_its_columns():
    old = st.PriceModel([], {"make_model": {}}, 0, 0, {}, {})
    assert old.features == list(st.LEGACY_FEATURES) and "source" not in old.features
    X, names, _ = st.pooled_features([car("autoplius", "1")], old.codes, old.features)
    assert X.shape == (1, len(st.LEGACY_FEATURES)) and names == list(st.LEGACY_FEATURES)
