"""Verdicts, your labels, and the report build end to end."""

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import autoplius_stats as st
import scraper_dashboard as dash
from collectors import watch as w
from collectors.store import Store
from collectors.verdicts import classify, label_agrees, load_labels, set_label

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# verdict rules
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kw, expected", [
    (dict(price=12_000), None),                               # at or above expected
    (dict(price=9_500), None),                                # cheap, but not under the low end
    (dict(price=2_000), "not a real price"),                  # under 30% of expected
    (dict(price=9_500, outlier=True), "not a real price"),    # outside its group
    (dict(price=7_000, declared=True), "damaged"),
    (dict(price=7_000, flags=["not running"]), "problem"),
    (dict(price=7_000, declared=True, flags=["Daužtas"]), "damaged"),
    (dict(price=7_000, read=True), "deal"),
    (dict(price=7_000), "unchecked"),
    (dict(price=None), None),
])
def test_classify(kw, expected):
    price = kw.pop("price")
    assert classify(price, 8_000, 10_000, **kw) == expected


def test_labels_agree_with_the_right_verdicts():
    assert label_agrees("deal", "deal") and not label_agrees("deal", "unchecked")
    assert label_agrees("damaged", "damaged") and label_agrees("damaged", "problem")
    assert label_agrees("fake", "not a real price") and not label_agrees("fake", "deal")


def test_watcher_uses_the_same_rules():
    assert w.verdict(7_000, 8_000, 10_000, {}, "BMW", "Tvarkingas", read=True)[0] == "deal"
    assert w.verdict(7_000, 8_000, 10_000, {"damage_type": "Daužtas"}, read=True)[0] == "damaged"


# --------------------------------------------------------------------------- #
# labels
# --------------------------------------------------------------------------- #
def test_labels_round_trip(tmp_path):
    con = sqlite3.connect(tmp_path / "t.db")
    set_label(con, "autoplius", "1", "deal")
    set_label(con, "autogidas", "1", "fake")
    set_label(con, "autoplius", "1", "damaged")               # changed your mind
    assert load_labels(con) == {("autoplius", "1"): "damaged", ("autogidas", "1"): "fake"}
    set_label(con, "autoplius", "1", "")                      # cleared
    assert load_labels(con) == {("autogidas", "1"): "fake"}
    with pytest.raises(ValueError):
        set_label(con, "autoplius", "1", "great")


@pytest.fixture
def dash_db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    Store(path).close()
    monkeypatch.setattr(dash, "DB_PATH", path)
    return path


def test_dashboard_label_endpoint(dash_db):
    assert dash.save_label({"source": "autogidas", "id": "0140173686", "label": "deal"}) \
        == "Marked as real deal."
    with sqlite3.connect(dash_db) as con:
        assert load_labels(con) == {("autogidas", "0140173686"): "deal"}


@pytest.mark.parametrize("payload", [
    {"source": "ebay", "id": "1", "label": "deal"},
    {"source": "autoplius", "id": "1; DROP TABLE listings", "label": "deal"},
    {"source": "autoplius", "id": 1, "label": "deal"},
    {"source": "autoplius", "id": "1", "label": "maybe"},
])
def test_dashboard_label_endpoint_rejects_bad_input(dash_db, payload):
    with pytest.raises(ValueError):
        dash.save_label(payload)


def test_new_deals_show_your_label(tmp_path):
    store = Store(tmp_path / "t.db")
    watch = w.WatchStore(store, "autoplius")
    row = {"id": "5", "title": "BMW 320", "year": 2015, "mileage_km": 1, "price_eur": 7000,
           "link": "https://x/5"}
    watch.record_deal(row, "bmw", "320", w.now_utc(), 8000, 10000, 12000, "deal", [])
    set_label(store.conn, "autoplius", "5", "fake")
    assert w.health(store.conn, "autoplius")["deals"][0]["label"] == "fake"
    store.close()


# --------------------------------------------------------------------------- #
# verdict summary and the whole report build
# --------------------------------------------------------------------------- #
def test_verdict_summary_counts_agreement():
    cars = [{"verdict": "deal", "label": "deal", "gone": True, "days": 2.0},
            {"verdict": "deal", "label": "fake", "gone": False, "days": None},
            {"verdict": "damaged", "label": "damaged", "gone": True, "days": 9.0},
            {"verdict": None, "label": None, "gone": False, "days": None}]
    rows = {r["verdict"]: r for r in st.verdict_summary(cars)}
    assert rows["deal"] == {"verdict": "deal", "n": 2, "labelled": 2, "agreed": 1,
                            "gone_n": 1, "gone_days": 2.0}
    assert rows["damaged"]["agreed"] == 1 and rows["not a candidate"]["n"] == 1


def _listing(source, ad_id, model, year, km, price, town="Kaunas"):
    return {"id": ad_id, "title": f"BMW {model}", "year": year, "price_eur": price,
            "mileage_km": km, "fuel": "Dyzelinas", "engine_l": "2.0", "power_kw": 140,
            "gearbox": "Automatinė", "location": town, "dealer": 0,
            "link": f"https://{source}.lt/skelbimai/bmw-{model}-{ad_id}.html"}


def test_report_build_end_to_end(tmp_path):
    db = tmp_path / "t.db"
    store = Store(db)
    ap, ag = store.site("autoplius"), store.site("autogidas")
    rows = [_listing("autoplius", str(i), "320", 2010 + i % 6, 100_000 + 7_919 * i,
                     6_000 + 300 * (i % 6) - 20 * (i % 7), town="Vilnius" if i % 2 else "Kaunas")
            for i in range(60)]
    ap.upsert_many(rows, "bmw", "320")
    twin = dict(rows[0], id="g1")                              # the same car on autogidas
    ag.upsert_many([twin, _listing("autogidas", "g2", "320", 2012, 140_000, 6_400)],
                   "bmw", "320")
    set_label(store.conn, "autoplius", "3", "deal")
    store.close()

    out = tmp_path / "report.html"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "autoplius_stats.py"), "--db", str(db),
         "-o", str(out), "--candidates-file", str(tmp_path / "cand.txt"), "--no-pooled",
         "--min-n", "3"],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    assert result.returncode == 0, result.stderr
    assert "1 cars are listed on two sites" in result.stdout

    data = json.loads(re.search(r"const DATA = (\{.*?\});\n", out.read_text(encoding="utf-8"),
                                re.S).group(1).replace("<\\/", "</"))
    cols = {k: i for i, k in enumerate(data["cols"])}
    assert {"verdict", "label", "aid", "src", "also"} <= set(cols)
    assert data["meta"]["duplicates"] == 1 and len(data["cars"]) == 61   # 62 listings, 1 merged
    labelled = [c for c in data["cars"] if c[cols["label"]]]
    assert [(c[cols["src"]], c[cols["aid"]], c[cols["label"]]) for c in labelled] == \
        [("autoplius", "3", "deal")]
    assert sum(r["labelled"] for r in data["meta"]["verdicts"]) == 1
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 1
