"""autogidas.lt: parsers against saved pages, make-level searches, ad pages."""

from types import SimpleNamespace

import pytest

from collectors import autogidas as ag
from collectors import autoplius as ap
from collectors import common, crawl
from collectors.store import Store


# --------------------------------------------------------------------------- #
# results pages
# --------------------------------------------------------------------------- #
def test_results_page_parses_every_card(page):
    rows = list(ag.scrape_page(page("autogidas/results_all.html")))
    assert len(rows) == 20 and len({r["id"] for r in rows}) == 20


def test_card_fields(page):
    rows = {r["id"]: r for r in ag.scrape_page(page("autogidas/results_all.html"))}
    assert rows["0140173686"] == {
        "id": "0140173686", "title": "Opel Zafira", "year": 2006, "price_eur": 850,
        "mileage_km": 266666, "fuel": "Dyzelinas", "engine_l": "1.9",
        "link": "https://autogidas.lt/skelbimas/opel-zafira-2006-m-vienaturis-0140173686.html",
        "power_kw": 88, "gearbox": "Mechaninė", "body": "Vienatūris",
        "location": "Vilkaviškis", "dealer": 0,
    }


def test_card_details_use_autoplius_spellings(page):
    rows = list(ag.scrape_page(page("autogidas/results_bmw_band.html")))
    assert {r["fuel"] for r in rows} <= {"Dyzelinas", "Benzinas", "Benzinas/dujos",
                                         "Benzinas/elektra", "Elektra", "Hibridas", ""}
    assert {r["gearbox"] for r in rows} <= {"Automatinė", "Mechaninė", None}
    assert all(1000 <= r["price_eur"] <= 3000 for r in rows)      # the price band held
    assert all(r["title"].startswith("BMW") for r in rows)


def test_warranty_is_not_mileage(page):
    rows = list(ag.scrape_page(page("autogidas/results_newest.html")))
    kona = [r for r in rows if r["title"] == "Hyundai Kona"][0]
    assert kona["mileage_km"] == ""            # "5 km / 36 mėn." is a warranty


def test_result_count(page):
    assert ag.result_count(page("autogidas/results_all.html")) == 26995
    assert ag.result_count(page("autogidas/results_bmw_band.html")) == 508
    assert ag.result_count("<html>Access denied</html>") is None


def test_card_ages_skip_the_viewed_badge(page):
    ages = ag.card_ages(page("autogidas/results_all.html"))
    assert len(ages) == 20 and ages["0140173686"] == 18


def test_make_list(page):
    makes = dict(ag.parse_makes(page("autogidas/results_all.html")))
    assert makes["BMW"] == 3509 and makes["Mercedes-Benz"] == 1787
    assert 80 < len(makes) and 25_000 < sum(makes.values()) < 30_000
    assert ag.parse_make_counts(page("autogidas/results_all.html"))["mercedes-benz"] == 1787


@pytest.mark.parametrize("text, body", [
    ("vienaturis", "Vienatūris"), ("visureigis--krosoveris", "Visureigis / Krosoveris"),
    ("Komercinis auto(su būda)", "Komercinis"), ("Kupė (Coupe)", "Kupė (Coupe)"),
    ("coupe", "Kupė (Coupe)"), ("Hečbekas", "Hečbekas"), ("nonsense", None), (None, None),
])
def test_body_names(text, body):
    assert ag.body_name(text) == body


# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #
def test_urls():
    assert ag.make_url("Mercedes-Benz") == \
        "https://autogidas.lt/skelbimai/automobiliai/?f_1%5B0%5D=Mercedes-Benz"
    banded = ag.band_url(ag.make_url("BMW"), 1000, 3000)
    assert "f_215=1000" in banded and "f_216=3000" in banded
    assert ag.page_url(ag.newest_url, 1) == ag.newest_url
    assert ag.page_url(ag.newest_url, 3).endswith("page=3")
    assert "page=" not in ag.band_url(ag.page_url(ag.newest_url, 5), 1, 2)


def test_damage_searches_leave_out_minor_defects():
    urls = ag.damaged_search_urls()
    assert len(urls) == 7 and all("f_46=" in u for u in urls)
    assert not any("smulk" in u for u in urls)


# --------------------------------------------------------------------------- #
# ad pages
# --------------------------------------------------------------------------- #
def test_ad_details(page):
    html = page("autogidas/ad_0140149347.html")
    assert ag.parse_ad_details(html) == {
        "power_kw": 66, "gearbox": "Mechaninė", "body": "Universalas",
        "damage_type": None,                    # "Be defektų" means no defect
        "reg_country": "Vokietija",
    }
    assert ag.parse_description(html).startswith("Hyundai i30cw")


class OneAnswer:
    def __init__(self, status, text):
        self.status, self.text, self.calls = status, text, 0

    def get(self, url, **kw):
        self.calls += 1
        assert kw["headers"]["User-Agent"]
        return SimpleNamespace(status_code=self.status, text=self.text,
                               apparent_encoding="utf-8", encoding=None)


def test_autogidas_ad_page_fetch(page):
    session = OneAnswer(200, page("autogidas/ad_0140149347.html"))
    description, details, ok, status = ag.fetch_ad_details(
        session, "https://autogidas.lt/skelbimas/x-0140149347.html")
    assert ok and status == 200 and details["power_kw"] == 66 and description


def test_autogidas_challenge_is_not_an_ad(tmp_path):
    blocks = common.BlockDetector(dump_path=tmp_path / "dump.html")
    session = OneAnswer(200, "<html><div class='g-recaptcha'></div></html>")
    _, _, ok, _ = ag.fetch_ad_details(session, "https://autogidas.lt/skelbimas/x-1.html", blocks)
    assert not ok and blocks.first_status == 200


@pytest.mark.parametrize("site, url", [
    (ag, "https://autogidas.lt/skelbimas/x-1.html"),
    (ap, "https://autoplius.lt/skelbimai/x-1.html"),
])
def test_a_removed_ad_is_not_a_refusal(tmp_path, site, url):
    blocks = common.BlockDetector(threshold=1, dump_path=tmp_path / "dump.html")
    _, _, ok, status = site.fetch_ad_details(OneAnswer(404, "<html>Not found</html>"), url, blocks)
    assert not ok and status == 404 and not blocks.tripped.is_set()


def test_reading_continues_past_a_sold_car(tmp_path):
    """Regression: one sold car (404) used to stop the whole night's ad reads."""
    store = Store(tmp_path / "t.db")
    db = store.site("autogidas")
    db.upsert_many([{"id": i, "title": "BMW 320", "year": 2015, "price_eur": 9000,
                     "mileage_km": 1, "fuel": "", "engine_l": "",
                     "link": f"https://autogidas.lt/skelbimas/x-{i}.html"} for i in ("1", "2")],
                   "bmw", "320")

    class FakeSite:
        @staticmethod
        def fetch_ad_details(session, url, blocks):
            if url.endswith("-1.html"):
                return None, {}, False, 404
            return "Tvarkingas", {"power_kw": 140}, True, 200

    blocks = common.BlockDetector(threshold=1, dump_path=tmp_path / "dump.html")
    assert crawl.fetch_missing_descriptions(FakeSite, db, None, blocks, 0) == 1
    rows = dict(store.conn.execute(
        "SELECT id, removed_at IS NOT NULL FROM listings ORDER BY id").fetchall())
    assert rows == {"1": 1, "2": 0} and db.descriptions_remaining() == 0
    store.close()


def test_ad_fetchers_refuse_other_hosts():
    with pytest.raises(ValueError):
        ag.fetch_ad_details(OneAnswer(200, ""), "https://example.com/skelbimas/x-1.html")
    with pytest.raises(ValueError):
        ap.fetch_ad_details(OneAnswer(200, ""), "https://example.com/skelbimai/x-1.html")


def test_autoplius_ad_page_fetch(page):
    # Regression: this path once failed on a missing HEADERS import.
    session = OneAnswer(200, page("autoplius/ad_31883145_damaged.html"))
    _, details, ok, _ = ap.fetch_ad_details(
        session, "https://autoplius.lt/skelbimai/bmw-320-31883145.html")
    assert ok and details["damage_type"] == "Pažeistas krušos"


# --------------------------------------------------------------------------- #
# make-level searches
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    # the canonical catalogue (Autoplius) knows these BMW models
    s.site("autoplius").sync_catalogue(
        [{"make": "bmw", "model": m, "make_id": 97, "model_id": i, "make_name": "BMW",
          "model_name": m.upper()} for i, m in enumerate(
            ["316", "318", "320", "520", "525", "x3", "x5", "x1", "530"], start=1)],
        ap.catalogue_url)
    yield s
    s.close()


def test_make_search_files_cars_under_canonical_models(store, page, monkeypatch):
    html = page("autogidas/results_bmw_band.html")
    monkeypatch.setattr(crawl, "fetch", lambda *a, **kw: html)
    db = store.site("autogidas")
    args = SimpleNamespace(max_pages=200, concurrency=1, delay=0)
    ids, complete = crawl.scrape_search(ag, ag.make_url("BMW"), "bmw", "*", db, None,
                                        common.BlockDetector(), args)
    assert complete and len(ids) == 20
    stored = dict(store.conn.execute(
        "SELECT title, model FROM listings WHERE source = 'autogidas'").fetchall())
    assert stored["BMW 318"] == "318" and stored["BMW X3 M Sport"] == "x3"
    assert "*" not in stored.values()


def test_make_search_rejects_other_makes(store, page, monkeypatch):
    html = page("autogidas/results_all.html")          # every make mixed
    monkeypatch.setattr(crawl, "fetch", lambda *a, **kw: html)
    db = store.site("autogidas")
    args = SimpleNamespace(max_pages=200, concurrency=1, delay=0)
    ids, _ = crawl.scrape_search(ag, ag.make_url("Opel"), "opel", "*", db, None,
                                 common.BlockDetector(), args)
    titles = [t for (t,) in store.conn.execute("SELECT title FROM listings WHERE source='autogidas'")]
    assert titles and all(t.startswith("Opel") for t in titles)


def test_make_catalogue_sync_has_no_duplicates(store):
    db = store.site("autogidas")
    entries = [{"make": "bmw", "model": "*", "make_id": None, "model_id": None,
                "make_name": "BMW", "model_name": "*"}]
    assert db.sync_catalogue(entries, ag.catalogue_url) == (0, 1)
    assert db.sync_catalogue(entries, ag.catalogue_url) == (1, 0)
    assert [u for u, _, _ in db.pending_models(7, False)] == [ag.make_url("BMW")]
    assert db.models_missing_ids() == 0      # makes have no ids by design


def test_newest_url_is_the_sites_newest_first_sort(page):
    # On autogidas "naujausi_asc" is "Naujausi viršuje" (newest first) and
    # "naujausi_desc" is "Seniausi viršuje" (oldest first). The saved page was
    # fetched with _desc: the watcher must refuse to walk it.
    html = page("autogidas/results_newest.html")
    assert '<option value="?f_50=naujausi_asc">Naujausi viršuje' in html
    assert ag.newest_url.endswith("f_50=naujausi_asc")
    assert ag.is_newest_first(html) is False
    assert ag.is_newest_first(html.replace(
        '"?f_50=naujausi_desc" selected="selected"', '"?f_50=naujausi_desc"').replace(
        '"?f_50=naujausi_asc"', '"?f_50=naujausi_asc" selected="selected"')) is True
    assert ag.is_newest_first("<html>no sort box</html>") is None
