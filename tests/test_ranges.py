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
