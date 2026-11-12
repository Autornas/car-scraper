"""Report pieces the watcher depends on: 'gone' detection and the saved model."""

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

import autoplius_stats as st


def car(i, model, seen, first=None, count=2):
    return {"source": "autoplius", "id": str(i), "make": "bmw", "model": model,
            "first_seen": (first or seen).isoformat(), "last_seen": seen.isoformat(),
            "seen_count": count}


def test_gone_is_measured_against_the_full_scrape():
    scrape = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    cars = [
        car(1, "320", scrape - timedelta(minutes=10)),   # seen in the scrape
        car(2, "320", scrape - timedelta(days=3)),       # not found by it: gone
        car(3, "320", scrape + timedelta(days=2)),       # inserted by the watcher later
        car(4, "x7", scrape + timedelta(days=2)),        # model never fully scraped
    ]
    st.mark_disappeared(cars, {("autoplius", "bmw", "320"): scrape})
    assert [c["gone"] for c in cars] == [False, True, False, False]


def test_a_new_watcher_ad_does_not_make_the_model_look_sold():
    scrape = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    cars = [car(i, "320", scrape - timedelta(minutes=5)) for i in range(5)]
    cars.append(car(99, "320", scrape + timedelta(days=4)))
    st.mark_disappeared(cars, {("autoplius", "bmw", "320"): scrape})
    assert not any(c["gone"] for c in cars)


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    pytest.importorskip("lightgbm")
    rng = random.Random(1)
    cars = []
    for i in range(400):
        model = rng.choice(["320", "520"])
        year = rng.randint(2008, 2020)
        km = rng.randint(50, 350) * 1000
        base = (9000 if model == "320" else 14000) * 1.12 ** (year - 2014) * (1 - km / 900_000)
        cars.append({
            "id": str(i), "make": "bmw", "model": model, "gen": None, "band": "2008-2020",
            "fuel": "Dyzelinas", "gearbox": rng.choice(["Automatinė", "Mechaninė"]),
            "body": "Sedanas", "year": year, "mileage_km": km, "engine_l": "2.0",
            "power_kw": 135, "dealer": 0, "damaged": 0, "suspect_declared": False,
            "suspect": False, "price_outlier": False,
            "price_eur": round(base * math.exp(rng.gauss(0, 0.1))),
        })
    directory = tmp_path_factory.mktemp("price_model")
    summary = st.fit_pooled(cars, folds=3, save_dir=directory)
    return cars, summary, directory


def test_saved_model_round_trip(trained):
    cars, summary, directory = trained
    assert summary["saved"] == str(directory)
    model = st.PriceModel.load(directory)
    assert model is not None and model.info["n"] == len(cars)
    lo, mid, hi = model.price({**cars[0], "gen_key": None})
    assert lo <= mid <= hi
    # newer and less driven is worth more
    old = model.price({**cars[0], "year": 2009, "mileage_km": 300_000})
    new = model.price({**cars[0], "year": 2019, "mileage_km": 60_000})
    assert new[1] > old[1]


def test_model_knows_only_trained_models(trained):
    _, _, directory = trained
    model = st.PriceModel.load(directory)
    assert model.knows({"make": "bmw", "model": "320"})
    assert not model.knows({"make": "aixam", "model": "e-minauto"})


def test_missing_model_loads_as_none(tmp_path):
    assert st.PriceModel.load(tmp_path) is None


def test_model_needs_enough_cars_of_a_make_model(trained):
    cars, _, directory = trained
    model = st.PriceModel.load(directory)
    n320 = sum(1 for c in cars if c["model"] == "320")
    assert model.info["model_counts"]["bmw|320"] == n320
    assert model.knows({"make": "bmw", "model": "320"}, min_cars=n320)
    assert not model.knows({"make": "bmw", "model": "320"}, min_cars=n320 + 1)
    # a model saved before the counts were kept: known if it was there at all
    model.info.pop("model_counts")
    assert model.knows({"make": "bmw", "model": "320"}, min_cars=10**6)
