"""Mileage that can't be right, the typical range, and similar cars in the report."""

import math

import pytest

import autoplius_stats as st
from collectors import autoplius as ap


def test_the_warranty_badge_is_not_the_mileage(page):
    # "3 000 km arba 2 mėn. pardavėjo garantija" sits before the real mileage
    rows = {r["id"]: r for r in ap.scrape_page(page("autoplius/results_newest.html"))}
    assert rows["32935628"]["mileage_km"] == 275_428


def test_a_card_without_a_parameter_line_still_skips_the_warranty():
    html = ('<a class="announcement-item" href="/skelbimai/skoda-octavia-2020-1.html">'
            '<div class="announcement-title">Skoda Octavia</div><span>2020</span>'
            '<strong>9 400 €</strong> 10 000 km arba 1 metų pardavėjo garantija '
            'Dyzelinas 260 000 km Tauragė</a>')
    assert next(ap.scrape_page(html))["mileage_km"] == 260_000


@pytest.mark.parametrize("km, age, low", [
    (326, 22, True),           # 326,000 typed in thousands
    (3000, 19, True),
    (15_000, 20, True),        # under 1,000 a year
    (30_000, 25, False),       # a kept-in-the-garage cabrio
    (500, 2, False),           # nearly new: fine
    (2_000, 3, False),
])
def test_implausibly_low_mileage(km, age, low):
    assert st.implausibly_low_km(km, age) is low


def test_typical_range_is_the_middle_half():
    q25, q75 = st.typical_range(700, 1250, 2200)
    assert 700 < q25 < 1250 < q75 < 2200
    # in log space p25 sits 0.6745 / 1.2816 of the way from the median to p10
    assert math.log(1250 / q25) == pytest.approx(math.log(1250 / 700) * 0.6745 / 1.2816)
    assert st.typical_range(None, 1250, 2200) == (None, None)


def car(i, year, km, price, fuel="Dyzelinas", **kw):
    return {"id": str(i), "make": "volvo", "model": "v70", "year": year, "mileage_km": km,
            "price_eur": price, "fuel": fuel, **kw}


def test_similar_cars_are_same_model_near_year_and_mileage():
    target = car(0, 2005, 300_000, 900)
    cars = [target,
            *[car(i, 2004 + i % 3, 280_000 + i * 1000, 1000 + i * 100) for i in range(1, 9)],
            car(20, 2010, 300_000, 4000),                    # year too far
            car(21, 2005, 500_000, 500),                     # mileage too far
            car(22, 2005, 300_000, 300, suspect=True),       # damaged: no comparison
            car(23, 2005, 300_000, 200, price_outlier=True),
            {**car(24, 2005, 300_000, 5000), "model": "xc90"}]
    st.attach_similar(cars)
    n, cheaper, median, p25, p75 = target["similar"]
    assert n == 8 and cheaper == 0 and median == 1450 and p25 <= median <= p75
    # a car with no neighbours at all
    lone = car(30, 1980, 100_000, 900)
    st.attach_similar([lone])
    assert lone["similar"] == (0, 0, None, None, None)


def test_unknown_mileage_compares_with_every_mileage():
    target = car(0, 2005, None, 900)
    others = [car(i, 2005, 100_000 * i, 1000) for i in range(1, 6)]
    st.attach_similar([target, *others])
    assert target["similar"][0] == 5


# --------------------------------------------------------------------------- #
# likely sale price, from the cars that left the sites
# --------------------------------------------------------------------------- #
def gone(i, mid, price, **kw):
    return {"id": str(i), "gone": True, "p_mid": mid, "price_eur": price, **kw}


def test_sale_ratio_needs_enough_sold_cars():
    assert st.sale_ratios([gone(i, 1000, 800) for i in range(29)]) == {}
    table = st.sale_ratios([gone(i, 1000, 800) for i in range(30)])
    assert table["all"] == (0.8, 30)


def test_sale_ratio_skips_flagged_fake_and_unsold_cars():
    cars = [gone(i, 10_000, 9_000) for i in range(30)]
    cars += [gone(100, 10_000, 1_000, suspect=True), gone(101, 10_000, 500, price_outlier=True),
             {"id": "102", "gone": False, "p_mid": 10_000, "price_eur": 100}]
    assert st.sale_ratios(cars)["all"] == (0.9, 30)


def test_sale_price_uses_its_price_band_when_the_band_has_enough():
    cheap = [gone(i, 1_000, 700) for i in range(40)]          # old cars: 0.7x
    dear = [gone(100 + i, 20_000, 19_000) for i in range(40)]  # newer: 0.95x
    table = st.sale_ratios(cheap + dear)
    assert st.likely_sale(1_500, table) == (pytest.approx(1_050), 40)
    assert st.likely_sale(30_000, table) == (pytest.approx(28_500), 40)
    # a band with too few sold cars falls back to all of them
    sale, n = st.likely_sale(8_000, table)
    assert n == 80 and sale == pytest.approx(8_000 * table["all"][0])
    assert st.likely_sale(None, table) == (None, 0)
    assert st.likely_sale(1_000, {}) == (None, 0)


def test_similar_cars_that_sold_are_counted_on_their_own():
    target = car(0, 2005, 300_000, 900)
    up = [car(i, 2005, 300_000, 2000) for i in range(1, 4)]
    sold = [car(i, 2005, 300_000, 1000 + i, gone=True) for i in range(10, 15)]
    st.attach_similar([target, *up, *sold])
    assert target["similar"][0] == 8                      # sold ones still compare
    assert target["sold_similar"] == (5, 1012)
    st.attach_similar([target, *up])
    assert target["sold_similar"] is None


def test_sale_price_prefers_similar_cars_that_sold():
    table = st.sale_ratios([gone(i, 1000, 800) for i in range(30)])
    assert st.likely_sale(10_000, table, (5, 7_500)) == (7_500, 5)
    # too few sold neighbours: the ratio again
    assert st.likely_sale(10_000, table, (4, 7_500)) == (pytest.approx(8_000), 30)
    # no price model at all, but enough sold neighbours
    assert st.likely_sale(None, {}, (6, 7_500)) == (7_500, 6)
