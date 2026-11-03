"""Multi-site database: migration from the single-site layout, per-site keys,
name aliases; plus the crawler's guards against rate limits and challenges."""

import sqlite3
from types import SimpleNamespace

import pytest

from collectors import autoplius as ap
from collectors import common, crawl
from collectors.store import Store, find_gone

V1_SCHEMA = """
CREATE TABLE listings (id TEXT PRIMARY KEY, make TEXT, model TEXT, title TEXT,
    year INTEGER, price_eur INTEGER, mileage_km INTEGER, fuel TEXT, engine_l TEXT,
    link TEXT, first_seen TEXT, last_seen TEXT, seen_count INTEGER DEFAULT 1,
    description TEXT, description_checked_at TEXT, power_kw INTEGER, gearbox TEXT,
    body TEXT, location TEXT, dealer INTEGER, damaged INTEGER, damage_type TEXT,
    reg_country TEXT);
CREATE INDEX idx_make_model ON listings(make, model);
CREATE TABLE price_history (id TEXT, seen_at TEXT, price_eur INTEGER);
CREATE INDEX idx_ph_id ON price_history(id);
CREATE TABLE models (url TEXT PRIMARY KEY, make TEXT, model TEXT, last_scraped TEXT,
    found INTEGER, was_capped INTEGER DEFAULT 0, make_id INTEGER, model_id INTEGER,
    make_name TEXT, model_name TEXT, enabled INTEGER DEFAULT 1);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
INSERT INTO listings (id, make, model, title, year, price_eur, first_seen, last_seen,
                      damaged) VALUES
    ('100', 'bmw', '320', 'BMW 320', 2015, 9000, '2026-10-28', '2026-10-28', 1),
    ('101', 'audi', 'a4', 'Audi A4', 2012, 6000, '2026-10-28', '2026-10-28', 0);
INSERT INTO price_history VALUES ('100', '2026-10-28', 9500), ('100', '2026-11-01', 9000);
INSERT INTO models (url, make, model, last_scraped, found, model_id)
    VALUES ('u1', 'bmw', '320', '2026-10-28', 1, 10941);
INSERT INTO meta VALUES ('catalogue_synced_at', '2026-11-03');
"""


@pytest.fixture
def v1_db(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as con:
        con.executescript(V1_SCHEMA)
    return path


def test_v1_database_is_migrated_with_a_backup(v1_db):
    store = Store(v1_db)
    con = store.conn
    assert con.execute("SELECT source, id, make, price_eur, damaged FROM listings "
                       "ORDER BY id").fetchall() == [
        ("autoplius", "100", "bmw", 9000, 1), ("autoplius", "101", "audi", 6000, 0)]
    assert con.execute("SELECT source, id, price_eur FROM price_history ORDER BY seen_at"
                       ).fetchall() == [("autoplius", "100", 9500), ("autoplius", "100", 9000)]
    assert con.execute("SELECT source, model_id FROM models").fetchall() == [("autoplius", 10941)]
    assert store.meta("catalogue_synced_at:autoplius") == "2026-11-03"
    assert store.meta("schema_version") == "2"
    store.close()
    backups = list(v1_db.parent.glob("old.db.pre-v2-*"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as con:      # the backup is the untouched v1 file
        assert con.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2
        assert "source" not in {r[1] for r in con.execute("PRAGMA table_info(listings)")}


def test_migration_runs_once(v1_db):
    Store(v1_db).close()
    Store(v1_db).close()
    assert len(list(v1_db.parent.glob("old.db.pre-v2-*"))) == 1


def row(ad_id, price=9000, title="Mercedes-Benz C220"):
    return {"id": ad_id, "title": title, "year": 2015, "price_eur": price,
            "mileage_km": 150000, "fuel": "Dyzelinas", "engine_l": "2.2", "link": ""}


def test_two_sites_can_share_an_ad_id(tmp_path):
    store = Store(tmp_path / "t.db")
    store.site("autoplius").upsert_many([row("555", 9000)], "mercedes-benz", "c220")
    store.site("autogidas").upsert_many([row("555", 8500)], "mercedes-benz", "c220")
    assert store.conn.execute(
        "SELECT source, price_eur FROM listings ORDER BY source").fetchall() == [
        ("autogidas", 8500), ("autoplius", 9000)]
    assert store.site("autogidas").is_known("555")
    assert not store.site("autogidas").is_known("556")
    store.close()


def test_aliases_store_a_sites_names_under_the_canonical_ones(tmp_path):
    store = Store(tmp_path / "t.db")
    ag = store.site("autogidas")
    ag.set_alias("mercedes-benz", "c-220", "mercedes-benz", "c220")
    ag.upsert_many([row("1")], "mercedes-benz", "c-220")
    ag.upsert_many([row("2")], "mercedes-benz", "e-300")          # no alias: kept as is
    assert store.conn.execute("SELECT id, model FROM listings ORDER BY id").fetchall() == [
        ("1", "c220"), ("2", "e-300")]
    assert ag.canonical("mercedes-benz", "c-220") == ("mercedes-benz", "c220")
    assert store.site("autoplius").canonical("mercedes-benz", "c-220") == \
        ("mercedes-benz", "c-220")                                # aliases are per site
    store.close()


def test_gone_is_per_site(tmp_path):
    store = Store(tmp_path / "t.db")
    for source in ("autoplius", "autogidas"):
        store.site(source).upsert_many([row("1")], "bmw", "320")
    store.conn.execute("UPDATE listings SET last_seen = '2026-11-01T00:00:00+00:00'")
    store.conn.execute("""INSERT INTO models (url, source, make, model, last_scraped)
                          VALUES ('u', 'autoplius', 'bmw', '320', '2026-11-03T00:00:00+00:00')""")
    store.conn.commit()
    assert find_gone(store) == [("autoplius", "1")]      # autogidas was never fully scraped
    store.close()


def test_damage_sweep_is_per_site(tmp_path):
    store = Store(tmp_path / "t.db")
    for source in ("autoplius", "autogidas"):
        store.site(source).upsert_many([row("1"), row("2")], "bmw", "320")
    assert store.site("autoplius").apply_damage_sweep({"1"}) == 1
    assert store.conn.execute(
        "SELECT source, id, damaged FROM listings ORDER BY source, id").fetchall() == [
        ("autogidas", "1", None), ("autogidas", "2", None),
        ("autoplius", "1", 1), ("autoplius", "2", 0)]
    store.close()


# --------------------------------------------------------------------------- #
# being polite
# --------------------------------------------------------------------------- #
class FakeSession:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    def get(self, url, **kw):
        self.calls += 1
        status, headers = self.answers.pop(0)
        return SimpleNamespace(status_code=status, headers=headers, text="<html>ok</html>",
                               apparent_encoding="utf-8", encoding=None)


@pytest.fixture
def no_waiting(monkeypatch):
    waits = []
    monkeypatch.setattr(common, "_pause", waits.append)
    return waits


def test_rate_limit_is_waited_out(no_waiting):
    session = FakeSession([(429, {"Retry-After": "12"}), (429, {}), (200, {})])
    assert common.fetch(session, "https://x/") == "<html>ok</html>"
    assert no_waiting == [12, common.BACKOFF_SECONDS[1]]


def test_rate_limit_gives_up_after_the_last_wait(no_waiting):
    blocks = common.BlockDetector(dump_path=common.DATA_DIR / "test_dump.html")
    session = FakeSession([(429, {})] * 10)
    assert common.fetch(session, "https://x/", blocks=blocks) == ""
    assert len(no_waiting) == len(common.BACKOFF_SECONDS)
    assert blocks.first_status == 429
    (common.DATA_DIR / "test_dump.html").unlink(missing_ok=True)


def test_forbidden_is_not_retried(no_waiting):
    session = FakeSession([(403, {})] * 5)
    assert common.fetch(session, "https://x/") == "" and session.calls == 1
    assert no_waiting == []


CHALLENGE = "<html><div class='g-recaptcha'></div>Please verify</html>"


def test_a_challenge_page_never_marks_a_model_empty(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(crawl, "fetch", lambda *a, **kw: CHALLENGE)
    blocks = common.BlockDetector(dump_path=tmp_path / "dump.html")
    args = SimpleNamespace(max_pages=200, concurrency=1, delay=0)
    ids, complete = crawl.scrape_search(ap, "https://x/", "bmw", "320",
                                        store.site("autoplius"), None, blocks, args)
    assert not complete and not ids
    store.close()


def test_a_challenge_page_never_clears_damage_flags(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    db = store.site("autoplius")
    db.upsert_many([row("1")], "bmw", "320")
    db.apply_damage_sweep({"1"})
    monkeypatch.setattr(crawl, "fetch", lambda *a, **kw: CHALLENGE)
    args = SimpleNamespace(max_pages=200, delay=0)
    blocks = common.BlockDetector(dump_path=tmp_path / "dump.html")
    assert crawl.damage_sweep(ap, db, None, blocks, args) is None
    assert store.conn.execute("SELECT damaged FROM listings").fetchone()[0] == 1
    store.close()
