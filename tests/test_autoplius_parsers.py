"""
Autoplius parsers against real pages saved in tests/fixtures/autoplius/.

The values asserted here were read off those exact pages. If Autoplius changes
its layout, refresh the fixture and these tests show which field broke.
"""

from collectors import autoplius as db
from collectors.common import resolve_model_from_title


def test_results_page_parses_every_card(page):
    rows = list(db.scrape_page(page("autoplius/results_bmw_320.html")))
    assert len(rows) == 20
    assert len({r["id"] for r in rows}) == 20


def test_results_card_fields(page):
    first = next(db.scrape_page(page("autoplius/results_bmw_320.html")))
    assert first == {
        "id": "32908972",
        "title": "BMW 320",
        "year": 2011,
        "price_eur": 3450,
        "mileage_km": 270000,
        "fuel": "Dyzelinas",
        "engine_l": "2.0",
        "link": "https://autoplius.lt/skelbimai/"
                "bmw-320-2-0-l-universalas-2011-dyzelinas-32908972.html",
        "power_kw": 135,
        "gearbox": "Automatinė",
        "body": "Universalas",
        "location": "Šiauliai",
        "dealer": 0,
    }


def test_results_card_details_are_mostly_filled(page):
    rows = list(db.scrape_page(page("autoplius/results_bmw_320.html")))
    assert sum(1 for r in rows if r["power_kw"]) >= 19
    assert all(r["gearbox"] in ("Automatinė", "Mechaninė") for r in rows)
    assert all(r["body"] for r in rows)
    assert all(r["location"] for r in rows)
    assert sum(r["dealer"] for r in rows) == 4


def test_newest_first_page_parses(page):
    rows = list(db.scrape_page(page("autoplius/results_newest.html")))
    assert len(rows) == 20
    assert all(r["price_eur"] != "" and r["year"] != "" for r in rows)


def test_result_count(page):
    assert db.result_count(page("autoplius/results_bmw_320.html")) == 683
    assert db.result_count(page("autoplius/results_bmw_320_damaged.html")) == 17
    assert db.result_count("<html>no count here</html>") is None


def test_damaged_search_page_lists_all_damaged(page):
    rows = list(db.scrape_page(page("autoplius/results_bmw_320_damaged.html")))
    assert len(rows) == 17


def test_ad_details_with_declared_defect(page):
    assert db.parse_ad_details(page("autoplius/ad_31883145_damaged.html")) == {
        "power_kw": 135,
        "gearbox": "Automatinė",
        "body": "Sedanas",
        "damage_type": "Pažeistas krušos",
        "reg_country": "JAV",
    }


def test_ad_details_without_defect(page):
    details = db.parse_ad_details(page("autoplius/ad_32908972.html"))
    assert details["damage_type"] is None
    assert details["reg_country"] == "Vokietija"
    assert details["power_kw"] == 135


def test_ad_description(page):
    text = db.parse_description(page("autoplius/ad_31883145_damaged.html"))
    assert text.startswith("2014 BMW 320i automatas.")
    assert db.parse_ad_details("<html></html>") == {}


def test_damaged_url_ors_every_defect_type():
    url = db.damaged_url("https://autoplius.lt/skelbimai/naudoti-automobiliai/bmw/320")
    for damage_id in db.DAMAGE_IDS:
        assert f"has_damaged_id[{damage_id}]={damage_id}" in url
    assert "10924" not in url            # "Be defektų" must never be included


def test_parse_price_refuses_lease_figures():
    assert db.parse_price("22 750 €") == 22750
    assert db.parse_price("22 750 € nuo 259 € / mėn.") == 22750
    assert db.parse_price("Mėnesio įmoka nuo: 359 € / mėn.") is None
    assert db.parse_price("no price") is None


def test_from_slug():
    title, year, fuel, engine = db.from_slug(
        "https://autoplius.lt/skelbimai/bmw-320-2-0-l-sedanas-2007-dyzelinas-31767371.html")
    assert (title, year, fuel, engine) == ("Bmw 320", 2007, "Dyzelinas", "2.0")


def test_resolve_model_from_title():
    models = ["3", "320", "320d", "x5"]
    assert resolve_model_from_title("BMW 320", "bmw", models) == "320"
    assert resolve_model_from_title("BMW 320d Touring", "bmw", models) == "320d"
    assert resolve_model_from_title("Audi A4", "bmw", models) is None
