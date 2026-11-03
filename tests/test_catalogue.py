"""Catalogue sync, id-based searches, refresh cadence and the damage sweep."""

from types import SimpleNamespace

import pytest

from collectors import autoplius as db
from collectors import crawl
from collectors.common import BlockDetector, name_slug, title_fits
from collectors.store import Store


# --------------------------------------------------------------------------- #
# catalogue page
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def catalogue():
    from conftest import FIXTURES
    return db.parse_catalogue((FIXTURES / "autoplius/catalogue.html").read_text(encoding="utf-8"))


def find(entries, make, model):
    return [e for e in entries if e["make"] == make and e["model"] == model]


def test_catalogue_has_every_make(catalogue):
    assert len(catalogue) > 3000
    assert len({e["make"] for e in catalogue}) > 200
    assert len({e["model_id"] for e in catalogue}) == len(catalogue)   # no repeats


def test_catalogue_ids(catalogue):
    assert find(catalogue, "bmw", "320")[0]["model_id"] == 10941
    assert find(catalogue, "mercedes-benz", "c220")[0] == {
        "make": "mercedes-benz", "model": "c220", "make_id": 67, "model_id": 10982,
        "make_name": "Mercedes-Benz", "model_name": "C220"}
    assert find(catalogue, "land-rover", "range-rover-sport")
    assert find(catalogue, "tesla", "model-3")


def test_catalogue_skips_other_and_keeps_plus_models(catalogue):
    assert not [e for e in catalogue if "kita" in e["model"] or "kita" in e["make"]]
    assert find(catalogue, "toyota", "prius") and find(catalogue, "toyota", "prius-plus")


def test_catalogue_of_a_refused_page_is_empty():
    assert db.parse_catalogue("") == []
    assert db.parse_catalogue("<html>Access denied</html>") == []


@pytest.mark.parametrize("name, key", [
    ("Mercedes-Benz", "mercedes-benz"), ("Land Rover", "land-rover"),
    ("A klasė", "a-klasė"), ("Prius+", "prius-plus"), ("RX 450h", "rx-450h"),
    ("ID.4", "id-4"),
])
def test_name_slug(name, key):
    assert name_slug(name) == key


def test_make_counts_from_home_page(page):
    counts = db.parse_make_counts(page("autoplius/home.html"))
    assert counts["bmw"] > 6000 and counts["mercedes-benz"] > 3000
    assert 40_000 < sum(counts.values()) < 60_000


# --------------------------------------------------------------------------- #
# models table
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s.site("autoplius")
    s.close()


def entry(make, model, model_id, make_id=1):
    return {"make": make, "model": model, "make_id": make_id, "model_id": model_id,
            "make_name": make.title(), "model_name": model.upper()}


def models(store):
    with store.lock:
        return store.conn.execute(
            """SELECT make, model, url, model_id, enabled, last_scraped, found
               FROM models ORDER BY model_id""").fetchall()


def test_sync_keeps_progress_of_existing_models(store):
    with store.lock:
        store.conn.execute(
            """INSERT INTO models (url, make, model, last_scraped, found)
               VALUES ('https://old/bmw/320', 'bmw', '320', '2026-10-28T00:00:00+00:00', 690)""")
        store.conn.commit()
    updated, added = store.sync_catalogue([entry("bmw", "320", 10941, 97),
                                           entry("bmw", "x7", 555, 97)], db.catalogue_url)
    assert (updated, added) == (1, 1)
    rows = models(store)
    assert rows[1][:2] == ("bmw", "320")
    assert rows[1][2] == db.id_url(97, 10941)                    # now searched by id
    assert rows[1][5:] == ("2026-10-28T00:00:00+00:00", 690)      # progress kept
    assert rows[0][:2] == ("bmw", "x7") and rows[0][5] is None    # new, unscraped


def test_resync_does_not_re_enable_switched_off_models(store):
    store.sync_catalogue([entry("bmw", "320", 1)], db.catalogue_url)
    with store.lock:
        store.conn.execute("UPDATE models SET enabled = 0")
        store.conn.commit()
    store.sync_catalogue([entry("bmw", "320", 1)], db.catalogue_url)
    assert models(store)[0][4] == 0


def test_one_name_with_two_ids_gives_two_rows(store):
    store.sync_catalogue([entry("volkswagen", "lt", 10987), entry("volkswagen", "lt", 30587)], db.catalogue_url)
    store.sync_catalogue([entry("volkswagen", "lt", 10987), entry("volkswagen", "lt", 30587)], db.catalogue_url)
    assert [r[3] for r in models(store)] == [10987, 30587]


def test_pending_models_cadence(store):
    store.sync_catalogue([entry("a", "busy-recent", 1), entry("a", "busy-old", 2),
                          entry("a", "empty-old", 3), entry("a", "empty-ancient", 4),
                          entry("a", "never", 5), entry("a", "switched-off", 6)], db.catalogue_url)
    with store.lock:
        for model_id, days, found in [(1, 2, 50), (2, 10, 50), (3, 10, 0), (4, 40, 0),
                                      (6, 40, 50)]:
            store.conn.execute(
                """UPDATE models SET found = ?,
                       last_scraped = datetime('now', ?) WHERE model_id = ?""",
                (found, f"-{days} days", model_id))
        store.conn.execute("UPDATE models SET enabled = 0 WHERE model_id = 6")
        store.conn.commit()
    due = {m for _, _, m in store.pending_models(7, False, 30)}
    assert due == {"busy-old", "empty-ancient", "never"}
    assert len(store.pending_models(7, True)) == 5                # force: all enabled


# --------------------------------------------------------------------------- #
# searches
# --------------------------------------------------------------------------- #
KNOWN = ["320", "320-gran-turismo", "318", "x5"]


@pytest.mark.parametrize("title, fits", [
    ("BMW 320", True),
    ("BMW 320 Gran Turismo", False),        # another known model of this make
    ("BMW X5", False),
    ("Audi A6", False),                     # another make: the site answered a bad search
    ("BMW Alpina B3", True),                # a name we don't know: trust the id search
])
def test_title_fits(title, fits):
    assert title_fits(title, "bmw", "320", KNOWN) is fits


def search_args(**kw):
    return SimpleNamespace(**{"max_pages": 200, "concurrency": 3, "delay": 0, **kw})


def test_small_model_costs_one_request(store, page, monkeypatch):
    store.sync_catalogue([entry("bmw", m, i, 97) for i, m in enumerate(KNOWN, start=1)], db.catalogue_url)
    html = page("autoplius/results_bmw_320_damaged.html")       # 17 BMW 320s, one page
    calls = []
    monkeypatch.setattr(crawl, "fetch", lambda s, url, **kw: calls.append(url) or html)
    ids, complete = crawl.scrape_search(db, "https://x/?model_id=1", "bmw", "320", store,
                                        None, BlockDetector(), search_args())
    assert complete and len(ids) == 17 and len(calls) == 1


def test_empty_search_stops_at_once(store, monkeypatch):
    html = '<span class="result-count"> (0)</span>' + \
        '<a class="announcement-item" href="/skelbimai/audi-a6-1.html">Audi A6 2015 9 000 €</a>'
    monkeypatch.setattr(crawl, "fetch", lambda *a, **kw: html)
    ids, complete = crawl.scrape_search(db, "https://x/", "bmw", "320", store, None,
                                        BlockDetector(), search_args())
    assert complete and not ids


# --------------------------------------------------------------------------- #
# damage sweep
# --------------------------------------------------------------------------- #
def test_damage_sweep_flags_and_clears(store):
    rows = [{"id": i, "title": "BMW 320", "year": 2015, "price_eur": 9000,
             "mileage_km": 1, "fuel": "", "engine_l": "", "link": ""} for i in "abcd"]
    store.upsert_many(rows, "bmw", "320")
    store.save_description("d", None, {"damage_type": "Variklio defektas"})
    with store.lock:
        store.conn.execute("UPDATE listings SET damaged = 1 WHERE id = 'c'")
        store.conn.commit()
    assert store.apply_damage_sweep({"a"}) == 2
    with store.lock:
        state = dict(store.conn.execute("SELECT id, damaged FROM listings"))
    # a: in the sweep; c: no longer listed as damaged; d: its own ad declared a defect
    assert state == {"a": 1, "b": 0, "c": 0, "d": 1}
