#!/usr/bin/env python3
"""
autoplius_stats.py — turn the scraped database into price baselines you can explore.

Reads `data/autoplius.db` and writes a self-contained HTML report:
  * group baselines (median / p25 / p75), by GENERATION *and year band*
  * click any group to see its actual cars, sorted by price
  * filters: price, year, mileage, fuel
  * cars whose asking price has been cut  (uses price_history)
  * how fast cars disappear = sold          (uses first_seen / last_seen)

    python scripts/autoplius_stats.py --open
    python scripts/autoplius_stats.py --min-n 10 --bucket 3

Generations
-----------
The editable generation table is stored in `data/generations.json`. It is a
best-effort list, boundary years are approximate (autoplius reports first
registration, which can lag production by a year), and your corrections make
every baseline tighter. Models with no entry fall back to year bands alone.

Grouping
--------
A generation is not a baseline on its own. Passat B8 runs 2014-2022 and its
median price goes from EUR 5,350 to EUR 16,500 across that span, so comparing
every B8 against one number just ranks them by age. Cars are therefore grouped
by generation AND a year band (--bucket), with thin bands merged into their
neighbour until each group clears --min-n.

These are ASKING prices, not sale prices, and nothing here can see damage,
trim, or service history.
"""

import argparse
import json
import math
import re
import sqlite3
import statistics
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from collectors.dupes import find_duplicates, save_duplicates
from collectors.store import Store
from settings import SETTINGS, project_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = project_path(SETTINGS["paths"]["data_dir"])
REPORTS_DIR = project_path(SETTINGS["paths"]["reports_dir"])
CFG = SETTINGS["report"]

DEF_MIN_PRICE, DEF_MAX_PRICE = 100, 500_000
MIN_YEAR, MAX_YEAR = 1950, datetime.now().year + 2

# Smallest group that gets its own price model. Below this the median is both
# safer and, on held-out cars, usually better.
MIN_FIT_N = 25

# Mileage that no odometer can honestly hold. Both are needed: MAX_KM catches
# 999999-style placeholders, MAX_KM_PER_YEAR catches a 2026 car with 308,000 km.
# Failing either drops the mileage only — the price is still a usable data point.
MAX_KM = 900_000
MAX_KM_PER_YEAR = 100_000

# Starter generation table. Each entry: [year_the_generation_STARTED, name].
# A car is assigned the latest generation whose start year <= its year.
STARTER_GENERATIONS = {
    "bmw": {
        "316|318|320|323|325|328|330|335|340": [
            [1990, "E36"], [1998, "E46"], [2005, "E90"], [2012, "F30"], [2019, "G20"]],
        "518|520|523|525|528|530|535|540|545|550": [
            [1995, "E39"], [2003, "E60"], [2010, "F10"], [2017, "G30"]],
        "630|635|640|645|650": [[2003, "E63"], [2011, "F12"], [2018, "G32"]],
        "730|735|740|745|750": [[1994, "E38"], [2001, "E65"], [2008, "F01"], [2015, "G11"]],
        "x3": [[2003, "E83"], [2010, "F25"], [2017, "G01"]],
        "x5": [[1999, "E53"], [2006, "E70"], [2013, "F15"], [2018, "G05"]],
        "x6": [[2008, "E71"], [2014, "F16"], [2019, "G06"]],
    },
    "audi": {
        "a3": [[1996, "8L"], [2003, "8P"], [2012, "8V"], [2020, "8Y"]],
        "a4|s4": [[1994, "B5"], [2000, "B6"], [2004, "B7"], [2007, "B8"], [2015, "B9"]],
        "a5|s5|a5-sportback": [[2007, "8T"], [2016, "F5"]],
        "a6|s6": [[1997, "C5"], [2004, "C6"], [2011, "C7"], [2018, "C8"]],
        "a8": [[1994, "D2"], [2002, "D3"], [2010, "D4"], [2017, "D5"]],
        "q5": [[2008, "8R"], [2017, "FY"]],
        "q7": [[2005, "4L"], [2015, "4M"]],
    },
    "volkswagen|vw": {
        "golf": [[1997, "Mk4"], [2003, "Mk5"], [2008, "Mk6"], [2012, "Mk7"], [2019, "Mk8"]],
        "passat": [[1996, "B5"], [2005, "B6"], [2010, "B7"], [2014, "B8"], [2023, "B9"]],
        "polo": [[1994, "6N"], [2001, "9N"], [2009, "6R"], [2017, "AW"]],
        "touareg": [[2002, "7L"], [2010, "7P"], [2018, "CR"]],
        "tiguan": [[2007, "5N"], [2016, "AD"], [2024, "Mk3"]],
    },
    "mercedes-benz|mercedes": {
        "c160|c180|c200|c220|c230|c250|c270|c280|c300|c320|c350": [
            [2000, "W203"], [2007, "W204"], [2014, "W205"], [2021, "W206"]],
        "e200|e220|e250|e270|e280|e300|e320|e350|e400": [
            [1995, "W210"], [2002, "W211"], [2009, "W212"], [2016, "W213"]],
        "s320|s350|s400|s500": [[1998, "W220"], [2005, "W221"], [2013, "W222"]],
        "ml250|ml270|ml280|ml320|ml350": [[1997, "W163"], [2005, "W164"], [2011, "W166"]],
    },
    "skoda": {
        "octavia": [[1996, "Mk1"], [2004, "Mk2"], [2012, "Mk3"], [2019, "Mk4"]],
        "superb": [[2001, "Mk1"], [2008, "Mk2"], [2015, "Mk3"], [2023, "Mk4"]],
        "fabia": [[1999, "Mk1"], [2007, "Mk2"], [2014, "Mk3"], [2021, "Mk4"]],
    },
    "opel": {
        "astra": [[1998, "G"], [2004, "H"], [2009, "J"], [2015, "K"], [2021, "L"]],
        "insignia": [[2008, "A"], [2017, "B"]],
        "vectra": [[1995, "B"], [2002, "C"]],
        "zafira": [[1999, "A"], [2005, "B"], [2011, "C"]],
    },
    "toyota": {
        "avensis": [[1997, "T220"], [2003, "T250"], [2009, "T270"]],
        "corolla": [[2001, "E120"], [2006, "E140"], [2013, "E170"], [2018, "E210"]],
        "rav4": [[2000, "XA20"], [2005, "XA30"], [2012, "XA40"], [2018, "XA50"]],
        "yaris": [[1999, "XP10"], [2005, "XP90"], [2011, "XP130"], [2020, "XP210"]],
    },
    "ford": {
        "focus": [[1998, "Mk1"], [2004, "Mk2"], [2011, "Mk3"], [2018, "Mk4"]],
        "mondeo": [[2000, "Mk3"], [2007, "Mk4"], [2014, "Mk5"]],
        "fiesta": [[2002, "Mk5"], [2008, "Mk6"], [2017, "Mk7"]],
    },
    "volvo": {
        "v70|s70": [[2000, "P2"], [2007, "P3"]],
        "v60|s60": [[2000, "P2"], [2010, "P3"], [2018, "SPA"]],
        "xc90": [[2002, "Mk1"], [2015, "Mk2"]],
        "xc60": [[2008, "Mk1"], [2017, "Mk2"]],
    },
    "nissan": {
        "qashqai": [[2006, "J10"], [2013, "J11"], [2021, "J12"]],
        "x-trail|xtrail": [[2001, "T30"], [2007, "T31"], [2013, "T32"]],
    },
    "renault": {
        "megane": [[2002, "Mk2"], [2008, "Mk3"], [2015, "Mk4"]],
        "laguna": [[2001, "Mk2"], [2007, "Mk3"]],
    },
    "peugeot": {
        "308": [[2007, "T7"], [2013, "T9"], [2021, "P5"]],
        "508": [[2010, "Mk1"], [2018, "Mk2"]],
    },
}


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return float(s[lo] + (s[hi] - s[lo]) * (k - lo))


def ols(X, y):
    """Least squares by normal equations + Gauss-Jordan. None if singular."""
    n, p = len(X), len(X[0])
    A = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(p)]
         + [sum(X[i][a] * y[i] for i in range(n))] for a in range(p)]
    for col in range(p):
        piv = max(range(col, p), key=lambda r: abs(A[r][col]))
        if abs(A[piv][col]) < 1e-9:
            return None
        A[col], A[piv] = A[piv], A[col]
        for r in range(p):
            if r != col:
                f = A[r][col] / A[col][col]
                for cc in range(col, p + 1):
                    A[r][cc] -= f * A[col][cc]
    return [A[i][p] / A[i][i] for i in range(p)]


def _design(mem, y0, use_year):
    """Intercept + mileage, plus year only where the group actually spans years.
    At --bucket 1 a group is a single year, so that column would be constant and
    the normal equations singular; mileage alone still earns its keep there."""
    return [[1.0, c["mileage_km"] / 100_000] + ([float(c["year"] - y0)] if use_year else [])
            for c in mem]


def _abs_pct(pred_log, mem):
    return [abs(math.exp(p) - c["price_eur"]) / c["price_eur"] for p, c in zip(pred_log, mem)]


def fit_group(members, folds=5):
    """
    Fit log(price) ~ mileage + year inside one group, and keep the fit only where
    cross-validation shows it beating that same group's median.

    Stopping at two predictors is deliberate. Adding fuel and engine size lowers
    IN-SAMPLE error, which is why it looks tempting, but on held-out cars it
    loses: km+year scored 16.4% median error and beat the plain median in 35 of
    46 groups, against 16.8% (23/38) with fuel and 16.3% (15/31) with engine too.
    Groups here are too small to identify that many coefficients, so fuel and
    engine are reported as breakdowns instead of folded into the prediction.

    Folds are taken by position rather than at random so a rerun on unchanged
    data produces an identical report.
    """
    mem = [c for c in members if c["mileage_km"] and c["price_eur"] > 0]
    if len(mem) < MIN_FIT_N or len({c["mileage_km"] for c in mem}) < 5:
        return None
    y0 = min(c["year"] for c in mem)
    use_year = len({c["year"] for c in mem}) > 1
    logp = [math.log(c["price_eur"]) for c in mem]
    X = _design(mem, y0, use_year)

    fit_err, med_err = [], []
    for f in range(folds):
        tr = [i for i in range(len(mem)) if i % folds != f]
        te = [i for i in range(len(mem)) if i % folds == f]
        if len(tr) < 8 * len(X[0]) or not te:
            return None
        b = ols([X[i] for i in tr], [logp[i] for i in tr])
        if not b:
            return None
        te_mem = [mem[i] for i in te]
        fit_err += _abs_pct([sum(bi * xi for bi, xi in zip(b, X[i])) for i in te], te_mem)
        med_err += _abs_pct([statistics.median([logp[i] for i in tr])] * len(te), te_mem)

    cv_fit, cv_med = statistics.median(fit_err) * 100, statistics.median(med_err) * 100
    if cv_fit >= cv_med:                      # the median wins here; say so and move on
        return None
    b = ols(X, logp)
    if not b:
        return None
    pred = [sum(bi * xi for bi, xi in zip(b, row)) for row in X]
    mean_lp = statistics.mean(logp)
    ss_res = sum((a - p) ** 2 for a, p in zip(logp, pred))
    ss_tot = sum((a - mean_lp) ** 2 for a in logp)
    kms = [c["mileage_km"] for c in mem]
    return {
        "b0": b[0], "km": b[1], "yr": b[2] if use_year else 0.0, "y0": y0, "n": len(mem),
        "km_lo": min(kms), "km_hi": max(kms),
        "err": round(cv_fit, 1), "err_median": round(cv_med, 1),
        "r2": round(1 - ss_res / ss_tot, 2) if ss_tot else 0.0,
        # Readable slopes: what 100,000 km and one model year are worth here.
        "km_pct": round((math.exp(b[1]) - 1) * 100, 1),
        "yr_pct": round((math.exp(b[2]) - 1) * 100, 1) if use_year else None,
    }


def expected_price(fit, km, year):
    """Model price for one car. Mileage is clamped to the range the fit saw, so a
    freak odometer can never produce a freak expectation."""
    if not fit or not km:
        return None
    km = min(max(km, fit["km_lo"]), fit["km_hi"])
    return math.exp(fit["b0"] + fit["km"] * km / 100_000 + fit["yr"] * (year - fit["y0"]))


# --------------------------------------------------------------------------- #
# Damage / problem flags from the seller's own words
# --------------------------------------------------------------------------- #
# Phrases that DENY a problem are removed first, so "be defektų", "nedaužtas"
# or "jokių gedimų" never count as hits. \b keeps "nedaužtas" from matching
# "daužt" too, but the explicit list is what makes the intent readable.
NEGATIONS_RE = re.compile(
    r"\b(be|nėra|nera|jokių|jokiu|neturi|nebuvo|neturėjo)\s+(jokių\s+|jokiu\s+)?"
    r"(defekt|gedim|daužt|dauzt|pažeidim|pazeidim|avarij)\w*"
    r"|\bned[aа]u[žz]\w*|\bnebuvo\s+daužt\w*|\bnot\s+damaged\b|\bno\s+damage\b",
    re.I)

FLAG_PATTERNS = {
    "damaged": r"\bdaužt\w*|\bdauzt\w*|\bdaužyt\w*|po\s+avarijos|\bavarin\w*|\bdefekt\w*"
               r"|\bgedim\w*|\bsugedu\w*|\bsugedęs|\bremontui\b|reikia\s+remonto"
               r"|reikalauja\s+remonto|\bбит(ый|ая|ое)\b|\bdamaged\b|\baccident\b",
    "not running": r"neužsiveda|neuzsiveda|nevažiuoja|nevaziuoja|nesiveda|не\s+заводится"
                   r"|\bnot\s+running\b|does\s+not\s+start|neveikia\s+variklis",
    "no inspection": r"\bbe\s+t\.?\s?a\.?(?=\W|$)|ta\s+negalioja|nėra\s+ta\b|be\s+techninės",
    "export": r"\beksportui\b|\bexport\b|\bэкспорт\w*",
    "parts": r"\bdalimis\b|\bardom\w*|\bna\s+zapchasti\b|на\s+запчасти|\bfor\s+parts\b",
    "lease transfer": r"lizing\w*\s+(perėmim|perimim|perleid)\w*|perimti\s+lizing\w*"
                      r"|perimamas\s+lizing\w*",
    "no documents": r"be\s+dokument\w*|be\s+registracijos|išregistruot\w*|neregistruot\w*",
}
FLAG_RES = {name: re.compile(p, re.I) for name, p in FLAG_PATTERNS.items()}


def text_flags(*texts):
    """Problem keywords found in a title / description. Edit FLAG_PATTERNS freely:
    flags are recomputed on every run, so nothing has to be re-fetched."""
    text = NEGATIONS_RE.sub(" ", " ".join(t for t in texts if t))
    return [name for name, rx in FLAG_RES.items() if rx.search(text)]


# --------------------------------------------------------------------------- #
# Pooled price model
# --------------------------------------------------------------------------- #
# One model over every car instead of one per group. The per-group fit above
# has to stop at km + year because a 40-car group can't identify more; a single
# gradient-boosted model sees all ~37k cars and can learn what power, gearbox,
# fuel, body and declared damage are worth, sharing what it learns across models.
#
# Three quantiles are fitted (10th / 50th / 90th percentile of log price), so
# every car gets a range, not just a point: a car under the 10th percentile is
# cheaper than 9 in 10 cars the model considers identical.
#
# Every car is priced OUT OF FOLD — by a model that never saw it. Trees fitted
# on a car will happily memorise its price, and then nothing looks cheap.
POOLED_QUANTILES = (0.1, 0.5, 0.9)
POOLED_CATEGORICAL = ("make_model", "gen_key", "fuel", "gearbox", "body", "source")
# What models saved before `source` existed were trained on.
LEGACY_FEATURES = ("make_model", "gen_key", "fuel", "gearbox", "body",
                   "year", "km", "engine", "kw", "dealer", "damaged")
POOLED_NUMERIC = ("year", "km", "engine", "kw", "dealer", "damaged")
IMPLAUSIBLE_SHARE = 0.3


def _float(value):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return float("nan")


def gen_key(c):
    """Generation if known, otherwise the year band the report grouped it in."""
    return f"{c['make']}|{c['model']}|{c.get('gen') or c.get('band')}"


def pooled_features(cars, codes=None, features=None):
    """
    Feature matrix for the pooled model. `codes` maps each categorical feature's
    values to integers. Training builds it from the cars; scoring new ads must
    pass the training's codes back in, or "BMW" would mean a different number.
    `features` picks and orders the columns: a saved model must be fed exactly
    the columns it was trained on. Returns (X, names, codes).
    """
    import numpy as np
    raw = {
        "make_model": [f"{c['make']}|{c['model']}" for c in cars],
        "gen_key": [c.get("gen_key") or gen_key(c) for c in cars],
        "fuel": [c["fuel"] or None for c in cars],
        "gearbox": [c.get("gearbox") or None for c in cars],
        "body": [c.get("body") or None for c in cars],
        "source": [c.get("source") or None for c in cars],
    }
    if codes is None:
        codes = {name: {v: i for i, v in enumerate(sorted({v for v in raw[name] if v}))}
                 for name in POOLED_CATEGORICAL}
    columns = {}
    for name in POOLED_CATEGORICAL:
        known = codes.get(name, {})
        columns[name] = [known.get(v, float("nan")) if v else float("nan") for v in raw[name]]
    numeric = {
        "year": [c["year"] for c in cars],
        "km": [c["mileage_km"] if c["mileage_km"] else float("nan") for c in cars],
        "engine": [_float(c["engine_l"]) for c in cars],
        "kw": [c.get("power_kw") or float("nan") for c in cars],
        "dealer": [c.get("dealer") if c.get("dealer") is not None else float("nan")
                   for c in cars],
        "damaged": [1 if c.get("suspect_declared") else
                    (0 if c.get("damaged") == 0 else float("nan")) for c in cars],
    }
    columns.update(numeric)
    names = list(features or (*POOLED_CATEGORICAL, *POOLED_NUMERIC))
    return np.array([columns[n] for n in names], dtype=float).T, names, codes


POOLED_PARAMS = dict(objective="quantile", learning_rate=0.05, num_leaves=63,
                     min_data_in_leaf=15, bagging_fraction=0.8, bagging_freq=1,
                     feature_fraction=0.9, cat_smooth=10, min_data_per_group=20,
                     deterministic=True, verbose=-1)
POOLED_ROUNDS = 500


def fit_pooled(cars, folds=5, seed=7, save_dir=None):
    """Out-of-fold p10 / p50 / p90 for every car. Returns a summary dict, or None
    when lightgbm is not installed (the per-group baselines are used instead).

    With `save_dir`, also fits the same three quantiles on ALL cars and saves
    them there (see PriceModel), so the watcher can price a new ad the moment
    it appears without refitting anything."""
    try:
        import lightgbm as lgb
        import numpy as np
    except ImportError:
        return None
    X, names, codes = pooled_features(cars)
    y = np.log(np.array([c["price_eur"] for c in cars], dtype=float))
    # Group price outliers (parts cars, typos, lease figures) don't teach the
    # model anything about what a car is worth; they are still priced below.
    train_ok = np.array([not c.get("price_outlier") for c in cars])
    # (Duplicates of a car on another site never get here: merge_duplicates
    # removed them, so each car is learned from once.)
    # Fold by a stable hash of the id, so a rerun on unchanged data is identical.
    fold = np.array([int(c["id"]) % folds if str(c["id"]).isdigit()
                     else hash(c["id"]) % folds for c in cars])
    params = {**POOLED_PARAMS, "seed": seed}
    cat_idx = [names.index(n) for n in POOLED_CATEGORICAL]
    pred = np.full((len(cars), len(POOLED_QUANTILES)), np.nan)
    for f in range(folds):
        tr, te = (fold != f) & train_ok, fold == f
        data = lgb.Dataset(X[tr], y[tr], categorical_feature=cat_idx, free_raw_data=False)
        for qi, q in enumerate(POOLED_QUANTILES):
            model = lgb.train({**params, "alpha": q}, data, num_boost_round=POOLED_ROUNDS)
            pred[te, qi] = model.predict(X[te])
    pred.sort(axis=1)                     # quantile crossing: keep lo <= mid <= hi
    clean = train_ok & np.array([not c["suspect"] for c in cars])
    # Quantile trees under-cover on unseen cars (~72% inside p10-p90, not 80%).
    # Shift each bound by the out-of-fold miss at its own quantile, so that on
    # clean cars exactly 10% really sit under "lo" and 10% over "hi".
    shift_lo = float(np.quantile((y - pred[:, 0])[clean], POOLED_QUANTILES[0]))
    shift_hi = float(np.quantile((y - pred[:, 2])[clean], POOLED_QUANTILES[2]))
    pred[:, 0] += shift_lo
    pred[:, 2] += shift_hi
    lo, mid, hi = np.exp(pred[:, 0]), np.exp(pred[:, 1]), np.exp(pred[:, 2])
    lo, hi = np.minimum(lo, mid), np.maximum(hi, mid)
    for c, a, b, d in zip(cars, lo, mid, hi):
        c["p_lo"], c["p_mid"], c["p_hi"] = float(a), float(b), float(d)
        # Under 30% of expected is a lease payment, a deposit, a typo or a parts
        # car — not a discount. Groups too small for an IQR fence never caught
        # these, so the model does it for every car.
        if c["price_eur"] < IMPLAUSIBLE_SHARE * b:
            c["price_outlier"] = True

    price = np.exp(y)
    err = np.abs(mid - price) / price
    inside = (price >= lo) & (price <= hi)
    summary = {
        "n": len(cars), "folds": folds,
        "err": round(float(np.median(err[clean])) * 100, 1),
        "coverage": round(float(inside[clean].mean()) * 100, 1),
        "features": list(names),
        "filled": {n: int(np.isfinite(X[:, i]).sum()) for i, n in enumerate(names)},
    }

    if save_dir is not None:
        full = lgb.Dataset(X[train_ok], y[train_ok], categorical_feature=cat_idx,
                           free_raw_data=False)
        boosters = [lgb.train({**params, "alpha": q}, full, num_boost_round=POOLED_ROUNDS)
                    for q in POOLED_QUANTILES]
        # A new ad has no report band yet; it gets the key its make/model/year
        # was trained under.
        year_keys = {f"{c['make']}|{c['model']}|{c['year']}": gen_key(c) for c in cars}
        PriceModel(boosters, codes, shift_lo, shift_hi, year_keys,
                   {"trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "n": int(train_ok.sum()), "err": summary["err"]}, names).save(save_dir)
        summary["saved"] = str(save_dir)
    return summary


class PriceModel:
    """
    The pooled model fitted on every car, saved to disk by autoplius_stats.py:

        data/price_model/q10.txt, q50.txt, q90.txt   LightGBM boosters
        data/price_model/meta.json                   category codes, range
                                                     calibration, year -> key

        model = PriceModel.load(DATA_DIR / "price_model")    # None if absent
        lo, mid, hi = model.price(car)                        # car = listing dict
    """
    FILES = ("q10.txt", "q50.txt", "q90.txt")

    def __init__(self, boosters, codes, shift_lo, shift_hi, year_keys, info, features=None):
        self.boosters, self.codes = boosters, codes
        self.features = list(features or LEGACY_FEATURES)
        self.shift_lo, self.shift_hi = shift_lo, shift_hi
        self.year_keys, self.info = year_keys, info

    def save(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for booster, name in zip(self.boosters, self.FILES):
            booster.save_model(str(directory / name))
        (directory / "meta.json").write_text(json.dumps({
            "codes": self.codes, "shift_lo": self.shift_lo, "shift_hi": self.shift_hi,
            "year_keys": self.year_keys, "info": self.info, "features": self.features,
            "quantiles": list(POOLED_QUANTILES)}), encoding="utf-8")

    @classmethod
    def load(cls, directory):
        directory = Path(directory)
        try:
            import lightgbm as lgb
            meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
            boosters = [lgb.Booster(model_file=str(directory / n)) for n in cls.FILES]
        except (ImportError, OSError, ValueError):
            return None
        return cls(boosters, meta["codes"], meta["shift_lo"], meta["shift_hi"],
                   meta["year_keys"], meta.get("info", {}), meta.get("features"))

    def knows(self, car):
        """Whether this make/model was in the training data. For one that wasn't,
        the price is a guess from unrelated cars and must not drive a verdict."""
        return f"{car['make']}|{car['model']}" in self.codes["make_model"]

    def price(self, car):
        """(lo, mid, hi) in EUR for one listing dict, or None without a year."""
        import numpy as np
        if not car.get("year"):
            return None
        car = dict(car)
        car.setdefault("gen_key", self.year_keys.get(
            f"{car['make']}|{car['model']}|{car['year']}"))
        X, _, _ = pooled_features([car], self.codes, self.features)
        lo, mid, hi = sorted(float(b.predict(X)[0]) for b in self.boosters)
        lo, mid, hi = (float(np.exp(lo + self.shift_lo)), float(np.exp(mid)),
                       float(np.exp(hi + self.shift_hi)))
        return min(lo, mid), mid, max(hi, mid)


def spark(prices, bins=12):
    """Coarse price histogram (0-100 per bin) for the inline distribution bar.
    Two humps here mean the group is really two different cars."""
    lo, hi = pct(prices, 0.02), pct(prices, 0.98)
    if hi is None or hi <= lo:
        return []
    counts = [0] * bins
    for p in prices:
        counts[min(bins - 1, max(0, int((p - lo) / (hi - lo) * bins)))] += 1
    top = max(counts)
    return [round(c / top * 100) for c in counts] if top else []


def breakdown(members, key, min_n=3, limit=6):
    """Median price per fuel / engine size. Descriptive only — see fit_group for
    why these are not predictors."""
    per = defaultdict(list)
    for c in members:
        if c[key]:
            per[c[key]].append(c["price_eur"])
    rows = [{"k": str(k), "n": len(v), "median": round(statistics.median(v))}
            for k, v in per.items() if len(v) >= min_n]
    return sorted(rows, key=lambda r: -r["n"])[:limit]


def depreciation(pts):
    """Annualised %/yr implied by the oldest vs newest median in a model series."""
    if len(pts) < 2:
        return None
    lo, hi = pts[0], pts[-1]
    span = hi["year"] - lo["year"]
    if span <= 0 or lo["median"] <= 0 or hi["median"] <= 0:
        return None
    return round((1 - (lo["median"] / hi["median"]) ** (1 / span)) * 100, 1)


def ensure_generations(path):
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            sys.exit(f"{path} is not valid JSON ({exc}). Fix or delete it.")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(STARTER_GENERATIONS, indent=2), encoding="utf-8")
    print(f"Wrote a starter generation table to {p.resolve()}")
    print("  Edit it to correct boundaries - models not listed use year bands.\n")
    return STARTER_GENERATIONS


def compile_generations(table):
    out = []
    for make_pat, models in table.items():
        for model_pat, gens in models.items():
            out.append((re.compile(f"^({make_pat})$", re.I),
                        re.compile(f"^({model_pat})$", re.I),
                        sorted(gens, key=lambda g: g[0])))
    return out


def generation_for(make, model, year, compiled):
    for make_re, model_re, gens in compiled:
        if make_re.match(make or "") and model_re.match(model or ""):
            name = None
            for start, gen in gens:
                if year >= start:
                    name = gen
                else:
                    break
            return name
    return None


def parse_ts(value):
    """Always returns an aware datetime, so rows written by different tools mix."""
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def load(db_path, compiled, min_price, max_price):
    if not Path(db_path).exists():
        sys.exit(f"No database at {db_path}. Run scripts/scrape.py first.")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    # Everything is fetched, including rows we will drop, so the skip counts below
    # describe the whole table rather than only what the WHERE clause let through.
    listing_columns = {
        row[1] for row in con.execute("PRAGMA table_info(listings)")
    }
    # Columns added by later versions of the scraper read as NULL on an
    # older database, so the report still builds before it is re-scraped.
    optional = ("description", "description_checked_at", "power_kw", "gearbox",
                "body", "location", "dealer", "damaged", "damage_type", "reg_country")
    extra_columns = ", ".join(
        name if name in listing_columns else f"NULL AS {name}" for name in optional)
    rows = con.execute(f"""SELECT source, id, make, model, title, year, price_eur, mileage_km,
                                  fuel, engine_l, link, first_seen, last_seen, seen_count,
                                  {extra_columns}
                           FROM listings""").fetchall()
    # Only the FIRST recorded price is ever used. SQLite takes the bare price_eur
    # from the same row that produced MIN(seen_at), so this needs no sort.
    first_price = {(r[0], r[1]): r[3] for r in
                   con.execute("SELECT source, id, MIN(seen_at), price_eur "
                               "FROM price_history GROUP BY source, id")}
    con.close()

    skipped = defaultdict(int)
    cars = []
    this_year = datetime.now().year
    for r in rows:
        if r["price_eur"] is None:
            skipped["no price"] += 1
            continue
        if r["year"] is None:
            skipped["no year"] += 1
            continue
        if not (min_price <= r["price_eur"] <= max_price):
            skipped["price out of range"] += 1
            continue
        if not (MIN_YEAR <= r["year"] <= MAX_YEAR):
            skipped["year out of range"] += 1
            continue
        c = dict(r)

        km = c["mileage_km"]
        if km is not None and km > 0:
            age = max(1, this_year - c["year"])
            if km > MAX_KM or km / age > MAX_KM_PER_YEAR:
                c["mileage_km"] = None
                skipped["impossible mileage"] += 1
        elif km is not None and km <= 0:
            c["mileage_km"] = None

        c["gen"] = generation_for(c["make"], c["model"], c["year"], compiled)

        # Declared = the site's own defect filter or the ad's "Defektai" row.
        # Keyword flags come from the seller's text and are only as complete as
        # the descriptions fetched, so they mark cars but never feed the model.
        c["suspect_declared"] = bool(c["damaged"] == 1 or c["damage_type"])
        c["flags"] = ([c["damage_type"] or "declared defect"] if c["suspect_declared"] else []) \
            + text_flags(c["title"], c["description"])
        c["suspect"] = bool(c["flags"])

        first = first_price.get((c["source"], c["id"]), c["price_eur"])
        c["first_price"] = first
        c["drop_pct"] = round((first - c["price_eur"]) / first * 100, 1) \
            if first and first > c["price_eur"] else 0.0
        cars.append(c)
    return cars, dict(skipped)


def assign_groups(cars, bucket, min_n):
    """
    Give every car a group key of generation + year band.

    A generation alone is too wide to compare against: Passat B8 spans 2014-2022
    and its median goes from EUR 5,350 to EUR 16,500 over that, so a single median
    would rank cars by age rather than by value. Bands start at --bucket years and
    are merged with the next band up whenever they are thinner than --min-n, which
    keeps the sparse tail years of a generation from becoming useless groups.
    """
    by_gen = defaultdict(list)
    for c in cars:
        by_gen[(c["make"], c["model"], c["gen"])].append(c)

    for (_, _, gen), members in by_gen.items():
        bands = defaultdict(list)
        for c in members:
            start = (c["year"] // bucket) * bucket if bucket > 1 else c["year"]
            bands[start].append(c)

        runs, run = [], []
        for start in sorted(bands):
            run.extend(bands[start])
            if len(run) >= min_n:
                runs.append(run)
                run = []
        if run:                       # thin tail: fold it into the band below
            if runs:
                runs[-1].extend(run)
            else:
                runs.append(run)

        for run in runs:
            years = [c["year"] for c in run]
            lo, hi = min(years), max(years)
            band = str(lo) if lo == hi else f"{lo}-{hi}"
            for c in run:
                c["band"] = band
                c["grp"] = f"{gen} {band}" if gen else band
    return cars


def model_scrape_times(db_path):
    """(source, make, model) -> when its last FULL scrape finished
    (models.last_scraped), under the canonical make/model listings use."""
    con = sqlite3.connect(db_path)
    try:
        out = {}
        for src, mk, md, ts in con.execute("""SELECT m.source, COALESCE(a.make, m.make), COALESCE(a.model, m.model),
                      m.last_scraped
               FROM models m LEFT JOIN model_aliases a
                 ON a.source = m.source AND a.site_make = m.make AND a.site_model = m.model"""):
            t = parse_ts(ts)
            if t and (out.get((src, mk, md)) is None or t > out[(src, mk, md)]):
                out[(src, mk, md)] = t
        return out
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def mark_disappeared(cars, scraped):
    """
    A listing is treated as gone (sold or withdrawn) if its model's last full
    scrape finished more than an hour after the listing was last seen, i.e.
    that scrape walked the whole model and didn't find it.

    The reference is the full scrape (`scraped`, from the models table), never
    the newest listing: the watcher inserts single new ads at any time, and one
    fresh BMW 320 must not make every other BMW 320 look sold. Models only the
    watcher has seen have no full scrape, so nothing in them is marked gone.
    """
    for c in cars:
        seen = parse_ts(c["last_seen"])
        first = parse_ts(c["first_seen"])
        # sites searched per make (autogidas) record one "*" model per make
        ref = scraped.get((c["source"], c["make"], c["model"])) \
            or scraped.get((c["source"], c["make"], "*"))
        c["gone"] = bool(seen and ref and (ref - seen).total_seconds() > 3600)
        # A listing seen in only ONE scrape carries no duration information:
        # first_seen == last_seen, so the difference is 0 however long it was
        # really up. Reporting that as "0 days listed" would turn ignorance into
        # a measurement, so it stays None and is excluded from the medians.
        c["days"] = round((seen - first).total_seconds() / 86400, 1) \
            if (seen and first and (c["seen_count"] or 0) >= 2) else None
    return cars


def merge_duplicates(cars, db_path):
    """
    The same car listed on two sites becomes one row: the cheaper listing
    stays (with the other attached as c["also"]), the other leaves the
    analysis, so it is neither counted twice in medians and the price model
    nor shown as a second "deal". Only listings still up are matched. The
    pairs are saved to the `duplicates` table for the dashboard and watcher.
    Returns (cars, number of pairs).
    """
    pairs = find_duplicates([c for c in cars if not c["gone"]])
    con = sqlite3.connect(db_path)
    try:
        save_duplicates(con, pairs)
    finally:
        con.close()
    hidden = set()
    for shown, other, _ in pairs:
        shown.setdefault("also", []).append(other)
        hidden.add(id(other))
    return [c for c in cars if id(c) not in hidden], len(pairs)


def build_groups(cars, min_n):
    buckets = defaultdict(list)
    for c in cars:
        buckets[(c["make"], c["model"], c["grp"])].append(c)

    groups = {}
    for key, members in buckets.items():
        if len(members) < min_n:
            continue
        # The fence goes in LOG space. Used prices are log-normal, so a symmetric
        # 1.5*IQR fence on raw euros is wrong in both directions: it over-trims the
        # expensive tail and its lower bound often lands below zero (Audi A4 B8:
        # -438 EUR), where it can never trim a 250 EUR parts car no matter how
        # absurd. In log space the same rule reads as a ratio and behaves.
        logs = [math.log(m["price_eur"]) for m in members]
        q1, q3 = pct(logs, 0.25), pct(logs, 0.75)
        iqr = q3 - q1
        lo_f, hi_f = math.exp(q1 - 1.5 * iqr), math.exp(q3 + 1.5 * iqr)
        # Trim members, not just prices, so a car excluded from its own group's
        # percentiles can be marked as such instead of quietly topping the
        # candidate list as a 90%-under-median "bargain".
        keep = [m for m in members if lo_f <= m["price_eur"] <= hi_f] or members
        inside = {id(m) for m in keep}
        for mm in members:
            mm["price_outlier"] = id(mm) not in inside
        kept = [m["price_eur"] for m in keep]
        miles = [m["mileage_km"] for m in members if m["mileage_km"]]
        years = [m["year"] for m in members]
        k25, k50, k75 = pct(kept, 0.25), statistics.median(kept), pct(kept, 0.75)
        err_median = round(statistics.median([abs(k50 - p) / p for p in kept]) * 100, 1)
        fit = fit_group(members)
        groups[key] = {
            "make": key[0], "model": key[1], "grp": key[2],
            "gen": members[0]["gen"] or "",
            "is_gen": bool(members[0]["gen"]),
            "band": f"{min(years)}-{max(years)}" if min(years) != max(years) else str(min(years)),
            # n counts every car in the group; the percentiles below are computed
            # after trimming price outliers, so n_stat is the sample behind them.
            "n": len(members),
            "n_stat": len(kept),
            "p25": round(k25),
            "median": round(k50),
            "p75": round(k75),
            "lo": min(kept), "hi": max(kept),
            "median_km": round(statistics.median(miles)) if miles else None,
            "spread": round((k75 - k25) / k50 * 100) if k50 else 0,
            # Typical error you accept by quoting the median as "the" price,
            # and the error of whichever baseline this group actually uses.
            "err_median": err_median,
            "err": fit["err"] if fit else err_median,
            "basis": "fit" if fit else "median",
            "fit": fit,
            "spark": spark(kept),
            "by_fuel": breakdown(members, "fuel"),
            "by_engine": breakdown(members, "engine_l"),
        }
    return groups


def sold_summary(cars, groups):
    """Do cars priced under their group median disappear faster? Ground truth."""
    cheap, dear = [], []
    for c in cars:
        if not c["gone"] or c["days"] is None:
            continue
        g = groups.get((c["make"], c["model"], c["grp"]))
        if not g:
            continue
        (cheap if c["price_eur"] < g["median"] else dear).append(c["days"])
    return {
        "gone": sum(1 for c in cars if c["gone"]),
        # Gone, but only ever seen once — we know they vanished, not how long
        # they were up. Counting them would report 0 days for every one.
        "unmeasured": sum(1 for c in cars if c["gone"] and c["days"] is None),
        "cheap_n": len(cheap), "dear_n": len(dear),
        "cheap_days": round(statistics.median(cheap), 1) if cheap else None,
        "dear_days": round(statistics.median(dear), 1) if dear else None,
    }


def model_series(cars, min_n):
    per = defaultdict(lambda: defaultdict(list))
    for c in cars:
        per[(c["make"], c["model"])][c["year"]].append(c["price_eur"])
    series = {}
    for (make, model), years in per.items():
        pts = []
        for y in sorted(years):
            prices = years[y]
            if len(prices) < min_n:
                continue
            pts.append({"year": y, "n": len(prices),
                        "lo": round(min(prices)), "hi": round(max(prices)),
                        "p25": round(pct(prices, 0.25)),
                        "median": round(statistics.median(prices)),
                        "p75": round(pct(prices, 0.75))})
        if len(pts) >= 3:
            for prev, cur in zip(pts, pts[1:]):        # year-on-year step
                cur["yoy"] = round((cur["median"] - prev["median"]) / prev["median"] * 100, 1) \
                    if prev["median"] else None
            series[f"{make}/{model}"] = {"pts": pts, "dep": depreciation(pts),
                                         "n": sum(p["n"] for p in pts)}
    return dict(sorted(series.items(), key=lambda kv: -kv[1]["n"]))


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autoplius price baselines</title>
<style>
  :root{color-scheme:light;
    --surface-1:#fcfcfb;--surface-2:#f3f3f1;--surface-3:#e9e9e6;--border:#dcdcd8;
    --text-primary:#0b0b0b;--text-secondary:#52514e;--text-muted:#7a7975;
    --series-1:#2a78d6;--band:#2a78d633;--good:#0b8a0b;--warn:#ec835a;--bad:#c93c3c;}
  @media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
    --surface-1:#1a1a19;--surface-2:#232322;--surface-3:#2d2d2b;--border:#3a3a38;
    --text-primary:#fff;--text-secondary:#c3c2b7;--text-muted:#96958c;
    --series-1:#3987e5;--band:#3987e540;--good:#3fbf3f;--warn:#ec835a;--bad:#ff8080;}}
  :root[data-theme="dark"]{color-scheme:dark;
    --surface-1:#1a1a19;--surface-2:#232322;--surface-3:#2d2d2b;--border:#3a3a38;
    --text-primary:#fff;--text-secondary:#c3c2b7;--text-muted:#96958c;
    --series-1:#3987e5;--band:#3987e540;--good:#3fbf3f;--warn:#ec835a;--bad:#ff8080;}
  *{box-sizing:border-box}
  [hidden]{display:none!important}
  html,body{height:100%}
  body{margin:0;background:var(--surface-1);color:var(--text-primary);
    font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  /* App layout: header, tabs and filters stay put; only the active table scrolls,
     inside its own box, so both of its scrollbars are always on screen. */
  .app{display:flex;flex-direction:column;height:100vh;padding:14px 18px 12px;gap:10px}
  .top{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 18px}
  h1{font-size:18px;margin:0}
  .sub{color:var(--text-muted);font-size:12px}
  .tiles{display:flex;flex-wrap:wrap;gap:8px}
  .tile{background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:6px 12px;
    display:flex;align-items:baseline;gap:8px;cursor:help}
  .tile .v{font-size:16px;font-weight:650;font-variant-numeric:tabular-nums}
  .tile .l{font-size:12px;color:var(--text-secondary)}
  .bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
  .tabs{display:flex;gap:2px;border-bottom:1px solid var(--border);overflow-x:auto;overflow-y:hidden;flex:none}
  .tab{padding:8px 14px;border:0;border-bottom:2px solid transparent;background:none;cursor:pointer;
    font:inherit;color:var(--text-secondary);margin-bottom:-1px;border-radius:0;white-space:nowrap}
  .tab:hover{color:var(--text-primary);background:none}
  .tab[aria-selected="true"]{color:var(--text-primary);border-bottom-color:var(--series-1);font-weight:600}
  .tab:focus-visible{outline:2px solid var(--series-1);outline-offset:-2px}
  input,select{background:var(--surface-2);color:var(--text-primary);border:1px solid var(--border);
    border-radius:7px;padding:6px 9px;font:inherit;font-size:13px}
  input[type=number]{width:96px}input.txt{width:190px}
  input[type=checkbox]{padding:0;vertical-align:-2px;margin:0 5px 0 0}
  button{background:var(--surface-2);color:var(--text-primary);border:1px solid var(--border);
    border-radius:7px;padding:6px 11px;font:inherit;font-size:13px;cursor:pointer}
  button:hover{background:var(--surface-3)}
  .toggle{display:inline-flex;align-items:center;font-size:13px;color:var(--text-secondary);
    background:var(--surface-2);border:1px solid var(--border);border-radius:7px;padding:5px 10px;cursor:pointer}
  .spacer{flex:1}
  .panel{flex:1;min-height:0;display:flex;flex-direction:column;gap:8px}
  .panel[hidden]{display:none}
  .tablebox{flex:1;min-height:0;overflow:auto;border:1px solid var(--border);border-radius:9px;background:var(--surface-2)}
  .split{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px}
  .pane{min-height:0;display:flex;flex-direction:column;gap:8px}
  .pane .head{font-size:13px;color:var(--text-secondary);min-height:20px}
  .pane .head strong{color:var(--text-primary);font-size:14px}
  .placeholder{margin:auto;color:var(--text-muted);text-align:center;padding:30px}
  table{border-collapse:collapse;width:100%;font-size:13px}
  th,td{padding:6px 10px;text-align:left;vertical-align:top;border-bottom:1px solid var(--border)}
  th{font-size:12px;color:var(--text-secondary);font-weight:600;user-select:none;white-space:nowrap;
     position:sticky;top:0;background:var(--surface-2);z-index:1;box-shadow:0 1px 0 var(--border)}
  th[data-k]{cursor:pointer}th[data-k]:hover{color:var(--text-primary)}
  th[aria-sort="descending"],th[aria-sort="ascending"]{color:var(--text-primary)}
  th[aria-sort="descending"]::after{content:" \25be"}
  th[aria-sort="ascending"]::after{content:" \25b4"}
  td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
  td .s{display:block;font-size:12px;color:var(--text-muted)}
  td.num .s{white-space:nowrap}
  td.main{min-width:180px}
  tbody tr.click{cursor:pointer}tbody tr.click:hover{background:var(--surface-3)}
  tbody tr.sel{background:var(--surface-3);box-shadow:inset 3px 0 0 var(--series-1)}
  tr.detail td{background:var(--surface-1);padding:10px 14px 14px}
  tr.detail[hidden]{display:none}
  .facts{display:flex;flex-wrap:wrap;gap:4px 18px;font-size:12px;color:var(--text-secondary);margin-bottom:8px}
  .facts b{color:var(--text-primary);font-weight:600}
  .desc{white-space:pre-wrap;max-height:220px;overflow:auto;font-size:13px;max-width:900px}
  a{color:var(--series-1)}
  a.open{white-space:nowrap;text-decoration:none;font-weight:600}
  .good{color:var(--good);font-weight:650}
  .neutral{color:var(--text-muted)}
  .pill{display:inline-block;font-size:11px;line-height:16px;padding:0 7px;border-radius:99px;
    background:var(--surface-3);color:var(--text-secondary);margin:0 0 0 5px;white-space:nowrap;font-weight:500}
  .pill.gen{background:var(--series-1);color:#fff}
  .pill.gone{background:var(--warn);color:#231a15}
  .pill.fit{background:var(--good);color:#fff}
  .pill.odd{background:var(--warn);color:#231a15}
  .pill.flag{background:var(--bad);color:#fff}
  .mini{display:flex;gap:14px;flex-wrap:wrap}
  .mini table{width:auto;min-width:200px;border:1px solid var(--border);border-radius:8px;background:var(--surface-2)}
  .mini td{border:0;padding:3px 8px}
  .mini caption{text-align:left;font-size:12px;color:var(--text-secondary);padding:0 0 4px 2px}
  details.help{font-size:12px;color:var(--text-secondary)}
  details.help summary{cursor:pointer;color:var(--text-muted);width:max-content}
  details.help[open]{background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:8px 12px}
  details.help p{margin:6px 0 0}
  .note{background:var(--surface-2);border:1px solid var(--border);border-left:3px solid var(--series-1);
    border-radius:8px;padding:10px 12px;font-size:13px;color:var(--text-secondary);margin-bottom:10px}
  .chartbox{flex:1;min-height:0;background:var(--surface-2);border:1px solid var(--border);border-radius:9px;padding:12px;display:flex}
  .chartbox svg{flex:1;min-height:0;height:100%}
  svg{display:block;width:100%;overflow:visible}
  .scrollpane{flex:1;min-height:0;overflow:auto}
  .tip{position:fixed;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);
    border-radius:8px;padding:8px 10px;font-size:12px;box-shadow:0 6px 20px #0003;opacity:0;transition:opacity .1s;z-index:9}
  .muted{color:var(--text-muted);font-size:12px}
  .count{color:var(--text-muted);font-size:12px}
  .empty{padding:22px;color:var(--text-muted);font-size:14px;text-align:center}
  /* Not wide enough for groups and their cars side by side: stack them, each
     keeping its own scroll box, instead of squeezing both into sideways scrolling. */
  @media (max-width:1280px){
    .split{grid-template-columns:1fr;grid-template-rows:minmax(0,1fr) minmax(0,1.3fr)}
  }
  /* Narrow screens: let the page scroll normally and cap each table instead. */
  @media (max-width:900px){
    .app{height:auto;min-height:100vh;padding:12px}
    .split{grid-template-rows:none}
    .tablebox{max-height:75vh;flex:none}
    .chartbox{height:340px;flex:none}
    input.txt{width:100%}
  }
</style></head><body><div class="app">
<header class="top">
  <h1>Autoplius price baselines</h1>
  <span class="sub" id="sub"></span>
</header>
<div class="tiles" id="tiles"></div>

<nav class="tabs" role="tablist" aria-label="Views">
  <button class="tab" role="tab" id="t-cars" aria-controls="p-cars" data-p="cars" aria-selected="true" tabindex="0">Deals</button>
  <button class="tab" role="tab" id="t-groups" aria-controls="p-groups" data-p="groups" aria-selected="false" tabindex="-1">Groups</button>
  <button class="tab" role="tab" id="t-chart" aria-controls="p-chart" data-p="chart" aria-selected="false" tabindex="-1">Price by year</button>
  <button class="tab" role="tab" id="t-drops" aria-controls="p-drops" data-p="drops" aria-selected="false" tabindex="-1">Price drops</button>
  <button class="tab" role="tab" id="t-sold" aria-controls="p-sold" data-p="sold" aria-selected="false" tabindex="-1">Sold speed</button>
</nav>

<div class="bar" id="filters">
  <input class="txt" id="q" placeholder="Make or model…" aria-label="Make or model">
  <input type="number" id="pmin" placeholder="€ min" aria-label="Minimum price">
  <input type="number" id="pmax" placeholder="€ max" aria-label="Maximum price">
  <input type="number" id="ymin" placeholder="Year from" aria-label="Earliest year">
  <input type="number" id="ymax" placeholder="Year to" aria-label="Latest year">
  <input type="number" id="kmax" placeholder="Max km" aria-label="Maximum mileage">
  <select id="fuel" aria-label="Fuel"><option value="">Any fuel</option></select>
  <select id="gearSel" aria-label="Gearbox"><option value="">Any gearbox</option></select>
  <select id="siteSel" aria-label="Site"><option value="">Any site</option></select>
  <button id="reset">Reset</button>
  <span class="count" id="fcount"></span>
</div>

<section class="panel" id="p-cars" role="tabpanel" aria-labelledby="t-cars">
  <div class="bar">
    <label class="toggle"><input type="checkbox" id="hideSus" checked>Hide flagged&nbsp;<span id="susCount" class="muted"></span></label>
    <label class="toggle"><input type="checkbox" id="hideOdd" checked>Hide price outliers&nbsp;<span id="oddCount" class="muted"></span></label>
    <span class="spacer"></span>
    <span class="count" id="carsCount"></span>
  </div>
  <details class="help"><summary>How to read this list</summary>
    <p><strong>Candidates, not deals.</strong> <span id="basisNote"></span></p>
    <p><strong>vs low end</strong> is how far the price sits under the bottom of that range:
    positive means cheaper than 9 in 10 comparable cars. Click a row for the seller's
    description and every detail.</p>
    <p><strong>Flagged</strong> = a defect declared on Autoplius (Daužtas, engine or gearbox
    defect, hail…) or problem words in the seller's text (not running, no TA, export, parts,
    lease transfer). Text flags exist only where a description was fetched — run
    <em>Check top candidates</em> in the dashboard, then rebuild the report.</p>
    <p><strong>Price outliers</strong> sit so far outside their group, or under 30% of expected,
    that they are almost always parts cars, typos or lease figures.</p>
  </details>
  <div class="tablebox"><table id="cars"><thead><tr>
    <th data-k="car">Car</th>
    <th class="num" data-k="year">Year</th>
    <th class="num" data-k="price">Price</th>
    <th class="num" data-k="base">Expected <span class="muted">/ range</span></th>
    <th class="num" data-k="vslo">vs low end</th>
    <th class="num" data-k="vs">vs expected</th>
    <th class="num" data-k="km">km</th>
    <th></th>
  </tr></thead><tbody></tbody></table></div>
</section>

<section class="panel" id="p-groups" hidden role="tabpanel" aria-labelledby="t-groups">
  <details class="help"><summary>How to read groups</summary>
    <p>Click a group to list its cars on the right. Groups are a generation <em>and</em> a
    year band, because one median across a whole generation only ranks cars by age.
    <strong>Spread</strong> is the p25–p75 width as a % of the median; a histogram with
    two humps means the group still mixes two kinds of car. <strong>±</strong> is how far
    the group baseline typically misses one car; <span class="pill fit">fit</span> marks a
    km+year model that beat the median in cross-validation.</p>
  </details>
  <div class="split">
    <div class="pane">
      <div class="head"><span class="count" id="groupsCount"></span></div>
      <div class="tablebox"><table id="groups"><thead><tr>
        <th data-k="model">Group</th><th class="num" data-k="n">Cars</th>
        <th class="num" data-k="median">Median <span class="muted">/ p25–p75</span></th>
        <th data-k="spread">Spread</th><th class="num" data-k="err">±</th>
      </tr></thead><tbody></tbody></table></div>
    </div>
    <div class="pane" id="drill"><div class="placeholder">Select a group to see its cars.</div></div>
  </div>
</section>

<section class="panel" id="p-chart" hidden role="tabpanel" aria-labelledby="t-chart">
  <div class="bar">
    <label class="muted" for="makeSel">Make</label><select id="makeSel"></select>
    <label class="muted" for="modelSel">Model</label><select id="modelSel"></select>
    <label class="toggle"><input type="checkbox" id="logY">Log scale</label>
    <span class="muted">median line, shaded band = p25–p75</span>
  </div>
  <div class="muted" id="chartTitle"></div>
  <div class="chartbox"><svg id="chart" viewBox="0 0 900 340" preserveAspectRatio="xMidYMid meet" role="img"
    aria-label="Median price by year"></svg></div>
</section>

<section class="panel" id="p-drops" hidden role="tabpanel" aria-labelledby="t-drops">
  <div class="bar"><span class="muted">Cars whose asking price was cut since first seen — a seller who drops the price is negotiable. Fills in as you re-scrape.</span>
    <span class="spacer"></span><span class="count" id="dropsCount"></span></div>
  <div class="tablebox"><table id="drops"><thead><tr>
    <th data-k="car">Car</th><th class="num" data-k="year">Year</th>
    <th class="num" data-k="first">First price</th>
    <th class="num" data-k="price">Now</th><th class="num" data-k="drop">Cut</th>
    <th class="num" data-k="base">Expected</th>
    <th class="num" data-k="days">Days listed</th><th></th>
  </tr></thead><tbody></tbody></table></div>
</section>

<section class="panel" id="p-sold" hidden role="tabpanel" aria-labelledby="t-sold">
  <div class="scrollpane" id="soldbox"></div>
</section>
</div><div class="tip" id="tip"></div>
<script>
const DATA = __DATA__;
const C = DATA.cols.reduce((a,k,i)=>(a[k]=i,a),{});
const CAP = 400;
const ENT = {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"};
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ENT[c]);
const eur = n => n==null ? "—" : "€"+Math.round(n).toLocaleString("en-US");
const num = n => n==null ? "—" : Math.round(n).toLocaleString("en-US");
/* Autoplius links are stored without their common prefix to keep the page small. */
const href = p => /^https?:/.test(String(p||"")) ? String(p)
  : "https://autoplius.lt/skelbimai/" + encodeURI(String(p||""));
const alink = p => p ? `<a class="open" href="${esc(href(p))}" target="_blank" rel="noopener">Open ↗</a>` : "—";
const cap = s => s ? s.charAt(0).toUpperCase()+s.slice(1) : "";
/* "a6", "xc60", "rav4" read best upper-case; "passat" reads best as "Passat". */
const mname = s => !s ? "" : (/\d/.test(s) || s.length<=2) ? s.toUpperCase() : cap(s);
/* One-line spec under the car name: what the price model compares on. */
const spec = c => [c[C.fuel], c[C.engine] ? c[C.engine]+" l" : "", c[C.kw] ? c[C.kw]+" kW" : "",
  c[C.gear], c[C.body]].filter(Boolean).map(esc).join(" · ");
/* Expandable detail row: every fact plus the seller's own text. */
const detailRow = (c, cols) => {
  const d = c[C.description];
  const facts = [
    ["Location", c[C.loc]], ["Seller", c[C.dealer]==null ? "" : c[C.dealer] ? "Dealer" : "Private"],
    ["Mileage", c[C.km]==null ? "" : num(c[C.km])+" km"], ["Body", c[C.body]],
    ["First price", c[C.first] && c[C.first]!==c[C.price] ? eur(c[C.first]) : ""],
    ["Days listed", c[C.days]], ["Flags", (c[C.flags]||"").split("|").join(", ")],
    ["Also listed", alsoList(c).map(o => `${o.src} ${o.price ? eur(o.price) : ""}`).join(", ")],
  ].filter(([,v]) => v!=null && v!=="").map(([k,v]) => `<span>${k}: <b>${esc(v)}</b></span>`).join("");
  return `<tr class="detail" hidden><td colspan="${cols}"><div class="facts">${facts}</div>${
    d ? `<div class="desc">${esc(d)}</div>`
      : `<div class="muted">${c[C.description_checked] ? "The seller wrote no description."
        : "Description not fetched yet — run “Check top candidates” in the dashboard."}</div>`}</td></tr>`;
};
/* Clicking a car row opens its detail row; links still just open. */
function wireExpand(id){
  document.querySelector(`#${id} tbody`).onclick = e => {
    if (e.target.closest("a")) return;
    const tr = e.target.closest("tr.click");
    if (tr && tr.nextElementSibling && tr.nextElementSibling.classList.contains("detail")) {
      tr.nextElementSibling.hidden = !tr.nextElementSibling.hidden;
      tr.classList.toggle("sel", !tr.nextElementSibling.hidden);
    }
  };
}
const gkey = c => c[C.make]+"|"+c[C.model]+"|"+c[C.grp];
const GMAP = {}; DATA.groups.forEach(g => GMAP[g.make+"|"+g.model+"|"+g.grp] = g);
const genPill = g => g && g.is_gen ? `${esc(g.gen)}<span class="pill gen">gen</span>` : "—";
const gonePill = c => c[C.gone] ? '<span class="pill gone">gone</span>' : "";
const oddPill = c => c[C.odd] ? '<span class="pill odd" title="price excluded from this'
  + ' group\'s percentiles as an outlier — usually a parts car, a typo, or a lease figure'
  + ' parsed as a price">outlier</span>' : "";
const flagPill = c => !c[C.flags] ? "" : c[C.flags].split("|").map(f =>
  `<span class="pill flag" title="declared on Autoplius or found in the seller's text">${esc(f)}</span>`).join("");
/* r.vs is "percent UNDER the baseline", so positive means cheaper. The sign has
   to be written out: a car above the baseline must not read as a discount. */
const vsText = v => v==null ? "—" : `${v>0?"−":"+"}${Math.abs(v).toFixed(1)}%`;
const vsCell = v => v==null ? '<td class="num neutral">—</td>'
  : `<td class="num ${v>0?"good":"neutral"}">${vsText(v)}</td>`;
/* Inline price histogram. Two humps = the group is still mixing two cars. */
const sparkSvg = s => !s||!s.length ? "" :
  `<svg viewBox="0 0 ${s.length*4-1} 18" width="${s.length*4-1}" height="18" aria-hidden="true">`
  + s.map((v,i)=>{const h=Math.max(1.5,v/100*18);
      return `<rect x="${i*4}" y="${18-h}" width="3" height="${h}" fill="var(--series-1)" opacity=".6"/>`;
    }).join("") + `</svg>`;
const pctSigned = v => (v>0?"+":"")+v+"%";
const basisTitle = g => g.fit
  ? `${g.fit.yr_pct==null?"mileage":"km+year"} model, better out of sample: ±${g.fit.err}% vs `
    + `±${g.fit.err_median}% for this group's median. R² ${g.fit.r2}. Each 100,000 km `
    + `${pctSigned(g.fit.km_pct)}${g.fit.yr_pct==null?"":", each model year "+pctSigned(g.fit.yr_pct)}. `
    + `Fitted on ${g.fit.n} cars with a mileage reading.`
  : `Group median. Typical miss ±${g.err_median}% per car. No km+year model: `
    + `too few cars with mileage, or the model lost to the median in cross-validation.`;

document.getElementById("sub").textContent =
  `${DATA.meta.cars.toLocaleString()} priced cars`
  + (DATA.meta.duplicates ? ` (${DATA.meta.duplicates.toLocaleString()} listed on two sites, counted once)` : "")
  + ` · ${DATA.meta.groups} groups of `
  + `${DATA.meta.min_n}+ cars · ${DATA.meta.gen_groups} grouped by generation · `
  + `${DATA.meta.skipped_total} listings skipped · generated ${DATA.meta.generated}`;
document.getElementById("tiles").innerHTML=[
  ["Listings",DATA.meta.cars.toLocaleString(),`${DATA.meta.models} models · ${DATA.meta.groups} groups`],
  ["Median price",eur(DATA.meta.median),"across every priced listing"],
  ["Baseline error","±"+DATA.meta.baseline_err+"%",
   `How far the baseline typically misses one car. Group medians alone would be ±${DATA.meta.median_err}%.`],
  DATA.meta.pooled
  ? ["Pooled model","±"+DATA.meta.pooled.err+"%",
     `One LightGBM model over all ${DATA.meta.pooled.n.toLocaleString()} cars, priced out of fold. `
     + `Typical miss on a clean car ±${DATA.meta.pooled.err}%; p10-p90 ranges calibrated to hold 80% of cars.`]
  : ["Modelled cars",DATA.meta.priced.toLocaleString(),
     `${DATA.meta.fitted} of ${DATA.meta.groups} groups earned a km+year price model; the rest use their median.`],
  ["Flagged",DATA.meta.suspect.toLocaleString(),
   `${DATA.meta.declared} declared defects on Autoplius, the rest from problem words in seller text. `
   + `The damaged-only search has covered ${DATA.meta.damage_checked.toLocaleString()} cars.`],
  ["Price cuts seen",DATA.meta.drops,"listings whose asking price fell since first seen"],
  ["Disappeared",DATA.meta.sold.gone,"listings that stopped appearing while their model was still scraped"],
].map(([l,v,t])=>`<div class="tile" title="${esc(t)}"><div class="v">${v}</div>
  <div class="l">${l}</div></div>`).join("");

/* ---------- sorting ---------- */
document.getElementById("basisNote").textContent = DATA.meta.pooled
  ? "Each car is compared with a pooled price model trained on every listing (make, model, "
    + "generation, year, mileage, fuel, engine, kW, gearbox, body, seller type, declared damage), "
    + "using a model that never saw that car. Expected is its median; Range is the 10th-90th "
    + "percentile for an identical car."
  : "Each car is compared with its group's km+year model, or the group median "
    + "(med) where no model earned its place. Install lightgbm for per-car price ranges.";
const SORT={groups:{k:"n",d:-1},cars:{k:"vslo",d:-1},drops:{k:"drop",d:-1},drilltbl:{k:"price",d:1}};
function sortRows(rows,s){
  return rows.sort((a,b)=>{
    const x=a[s.k],y=b[s.k];
    if(x==null&&y==null) return 0;
    if(x==null) return 1;            /* blanks always sink to the bottom */
    if(y==null) return -1;
    return (typeof x==="string" ? x.localeCompare(y) : x-y) * s.d;
  });
}
function wireSort(id,redraw){
  const s=SORT[id];
  document.querySelectorAll(`#${id} thead th`).forEach(th=>{
    const k=th.dataset.k;
    if(!k){th.removeAttribute("aria-sort");return;}
    th.setAttribute("aria-sort", k===s.k ? (s.d<0?"descending":"ascending") : "none");
    th.onclick=()=>{s.d=(s.k===k)?-s.d:-1;s.k=k;redraw();};
  });
}
function paint(id,rows,cells,cols,emptyMsg){
  sortRows(rows,SORT[id]);
  document.querySelector(`#${id} tbody`).innerHTML =
    rows.slice(0,CAP).map(cells).join("") ||
    `<tr><td colspan="${cols}" class="empty">${emptyMsg}</td></tr>`;
  const el=document.getElementById(id+"Count");
  if(el) el.textContent = rows.length>CAP
    ? `Showing the first ${CAP} of ${rows.length.toLocaleString()} rows — narrow the filters or sort to see the rest.`
    : `${rows.length.toLocaleString()} row${rows.length===1?"":"s"}.`;
}

/* ---------- filters ---------- */
const F={q:"",pmin:null,pmax:null,ymin:null,ymax:null,kmax:null,fuel:"",gear:"",site:""};
const SITES=[...new Set(DATA.cars.map(c=>c[C.src]).filter(Boolean))].sort();
document.getElementById("siteSel").innerHTML='<option value="">Any site</option>'
  +SITES.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join("");
document.getElementById("siteSel").hidden = SITES.length < 2;
/* Which site a car is from, shown only once there is more than one. */
const sitePill = c => SITES.length > 1 ? `<span class="pill">${esc(c[C.src])}</span>` : "";
/* The same car on another site: a link with that site's price. */
const alsoList = c => !c[C.also] ? [] : c[C.also].split("~").map(x => {
  const [src, price, link] = x.split("|"); return {src, price: +price || null, link}; });
const alsoPill = c => alsoList(c).map(o =>
  `<a class="pill" href="${esc(o.link)}" target="_blank" rel="noopener"
     title="the same car listed on ${esc(o.src)}">also on ${esc(o.src)} ${o.price ? eur(o.price) : ""} ↗</a>`
  ).join("");
const fuels=[...new Set(DATA.cars.map(c=>c[C.fuel]).filter(Boolean))].sort();
const gears=[...new Set(DATA.cars.map(c=>c[C.gear]).filter(Boolean))].sort();
document.getElementById("gearSel").innerHTML='<option value="">Any gearbox</option>'
  +gears.map(f=>`<option value="${esc(f)}">${esc(f)}</option>`).join("");
document.getElementById("fuel").innerHTML='<option value="">Any fuel</option>'
  +fuels.map(f=>`<option value="${esc(f)}">${esc(f)}</option>`).join("");
function readFilters(){
  const v=id=>{const e=document.getElementById(id).value; return e===""?null:+e;};
  F.q=document.getElementById("q").value.toLowerCase().trim();
  F.pmin=v("pmin");F.pmax=v("pmax");F.ymin=v("ymin");F.ymax=v("ymax");F.kmax=v("kmax");
  F.fuel=document.getElementById("fuel").value;
  F.site=document.getElementById("siteSel").value;
  F.gear=document.getElementById("gearSel").value;
  renderAll();
}
function match(c){
  if(F.q && !((c[C.make]+" "+c[C.model]).toLowerCase().includes(F.q))) return false;
  if(F.pmin!=null && c[C.price]<F.pmin) return false;
  if(F.pmax!=null && c[C.price]>F.pmax) return false;
  if(F.ymin!=null && c[C.year]<F.ymin) return false;
  if(F.ymax!=null && c[C.year]>F.ymax) return false;
  if(F.kmax!=null && (c[C.km]==null || c[C.km]>F.kmax)) return false;
  if(F.fuel && c[C.fuel]!==F.fuel) return false;
  if(F.gear && c[C.gear]!==F.gear) return false;
  if(F.site && c[C.src]!==F.site) return false;
  return true;
}
let debounceT;
const debounced=()=>{clearTimeout(debounceT);debounceT=setTimeout(readFilters,120);};
document.querySelectorAll("#filters input").forEach(e=>e.oninput=debounced);
document.getElementById("fuel").onchange=readFilters;
document.getElementById("gearSel").onchange=readFilters;
document.getElementById("siteSel").onchange=readFilters;
document.getElementById("hideOdd").onchange=()=>drawCars();
document.getElementById("hideSus").onchange=()=>drawCars();
document.getElementById("reset").onclick=()=>{
  clearTimeout(debounceT);
  document.querySelectorAll("#filters input,#filters select").forEach(e=>e.value="");
  readFilters();};

/* ---------- tabs ---------- */
const TABS=[...document.querySelectorAll(".tab")];
function selectTab(t){
  TABS.forEach(x=>{const on=x===t;
    x.setAttribute("aria-selected",on);x.tabIndex=on?0:-1;
    document.getElementById("p-"+x.dataset.p).hidden=!on;});
  /* The car filters mean nothing on the chart or the sold summary. */
  document.getElementById("filters").hidden = ["chart","sold"].includes(t.dataset.p);
  if(t.dataset.p==="chart") drawChart();
}
TABS.forEach((t,i)=>{
  t.onclick=()=>selectTab(t);
  t.onkeydown=e=>{
    let j=null;
    if(e.key==="ArrowRight")j=(i+1)%TABS.length;
    else if(e.key==="ArrowLeft")j=(i-1+TABS.length)%TABS.length;
    else if(e.key==="Home")j=0;
    else if(e.key==="End")j=TABS.length-1;
    if(j===null)return;
    e.preventDefault();TABS[j].focus();selectTab(TABS[j]);
  };
});

/* ---------- rows ---------- */
/* Cars that match the filters, counted per group, so the Groups tab can hide
   groups nothing matches and show "23 of 443" on the ones it does. */
let MATCHED={},matchedTotal=0;
function countMatches(){
  MATCHED={};matchedTotal=0;
  for(const c of DATA.cars){ if(!match(c))continue;
    matchedTotal++;const k=gkey(c);MATCHED[k]=(MATCHED[k]||0)+1;}
  document.getElementById("fcount").textContent =
    `${matchedTotal.toLocaleString()} of ${DATA.meta.cars.toLocaleString()} listings match`;
}
/* A car is judged against its expected price where the group earned a model, and
   against the group median otherwise. One number, with its basis always visible. */
function carRow(c,g){
  const base = c[C.exp] ?? (g?g.median:null);
  return {c,g,
    car:c[C.make]+" "+c[C.model],
    gen:g?g.gen:"", band:g?g.band:c[C.grp],
    year:c[C.year], price:c[C.price],
    base, modelled:c[C.exp]!=null,
    vs: base ? (base-c[C.price])/base*100 : null,
    lo:c[C.lo], hi:c[C.hi],
    vslo: c[C.lo] ? (c[C.lo]-c[C.price])/c[C.lo]*100 : null,
    kw:c[C.kw], gear:c[C.gear]||null,
    km:c[C.km], kmr:c[C.kmr], fuel:c[C.fuel]||null, engine:c[C.engine]||null,
    drop:c[C.drop]||null, first:c[C.first], days:c[C.days],
    median:g?g.median:null};
}
const baseCell = r => `<td class="num" title="${!r.modelled
    ? "group median — this group has no price model"
    : DATA.meta.pooled ? "median price the pooled model expects for this exact car"
    : "expected for this mileage and year, from the group's km+year model"}">${eur(r.base)}${
  r.modelled?"":'<span class="pill">med</span>'}</td>`;

/* ---------- groups ---------- */
let selGroup=null;
function drawGroups(){
  const rows=DATA.groups.filter(g=>MATCHED[g.make+"|"+g.model+"|"+g.grp]>0);
  paint("groups",rows,g=>{
    const k=g.make+"|"+g.model+"|"+g.grp, m=MATCHED[k];
    const cars=m===g.n ? `${g.n}` : `${m}<span class="s">of ${g.n}</span>`;
    const trimmed=g.n-g.n_stat;
    return `<tr class="click${selGroup===k?" sel":""}" data-k="${esc(k)}">
      <td class="main"><strong>${esc(cap(g.make))} ${esc(mname(g.model))}</strong>${
        g.is_gen?`<span class="pill gen">${esc(g.gen)}</span>`:""}<span class="s">${esc(g.band)}</span></td>
      <td class="num"${trimmed?` title="${trimmed} price outlier(s) excluded from the percentiles"`:""}>${cars}</td>
      <td class="num"><strong>${eur(g.median)}</strong><span class="s">${eur(g.p25)}–${eur(g.p75)}</span></td>
      <td title="price distribution, ${eur(g.lo)} to ${eur(g.hi)}">${sparkSvg(g.spark)}<span class="s">${g.spread}%</span></td>
      <td class="num" title="${esc(basisTitle(g))}">±${g.err}%${g.fit?'<span class="s"><span class="pill fit">fit</span></span>':""}</td></tr>`;
  },5,"no groups match the filters");
  document.querySelectorAll("#groups tbody tr.click").forEach(tr=>tr.onclick=()=>{
    selGroup=(selGroup===tr.dataset.k)?null:tr.dataset.k;drawGroups();drawDrill();});
}
wireSort("groups",()=>{drawGroups();});

function drawDrill(){
  const box=document.getElementById("drill");
  if(!selGroup||!GMAP[selGroup]){
    box.innerHTML=`<div class="placeholder">Select a group on the left to see its cars.</div>`;return;}
  const g=GMAP[selGroup];
  const rows=DATA.cars.filter(c=>gkey(c)===selGroup&&match(c)).map(c=>carRow(c,g));
  const shown=rows.length===g.n?`${g.n} cars`:`${rows.length} of ${g.n} cars match the filters`;
  box.innerHTML=`<div class="head"><strong>${esc(cap(g.make))} ${esc(mname(g.model))}
     ${esc(g.gen?g.gen+" ":"")}${esc(g.band)}</strong> · ${shown} · median ${eur(g.median)}
     · median ${num(g.median_km)} km <span class="count" id="drilltblCount"></span></div>
   <div class="tablebox">
   <details class="help" style="margin:8px 10px">
     <summary>Baseline and fuel / engine splits</summary>${groupStats(g)}</details>
   <table id="drilltbl"><thead><tr>
   <th class="num" data-k="year">Year</th><th data-k="price">Price</th>
   <th class="num" data-k="base">Expected</th><th class="num" data-k="vs">vs exp.</th>
   <th class="num" data-k="km">km</th><th></th>
   </tr></thead><tbody></tbody></table></div>`;
  paint("drilltbl",rows,r=>`<tr class="click"><td class="num">${r.year}</td>
      <td class="main"><strong>${eur(r.price)}</strong>${oddPill(r.c)}${gonePill(r.c)}${flagPill(r.c)}${sitePill(r.c)}${alsoPill(r.c)}
        ${r.drop?`<span class="pill">cut ${r.drop}%</span>`:""}<span class="s">${spec(r.c)}</span></td>
      ${baseCell(r)}${vsCell(r.vs)}
      <td class="num">${num(r.km)}${r.kmr?`<span class="s">${r.kmr}× group</span>`:""}</td>
      <td>${alink(r.c[C.link])}</td></tr>${detailRow(r.c,6)}`,
    6,"no cars match the filters");
  wireSort("drilltbl",()=>drawDrill());
  wireExpand("drilltbl");
}

/* What this group's prices are actually made of: how the baseline behaves, and
   the fuel / engine splits the single median hides. */
function miniTable(title,rows,med){
  if(!rows||!rows.length) return "";
  return `<table><caption>${esc(title)}</caption><tbody>`+rows.map(r=>{
    const d=med?Math.round((r.median-med)/med*100):0;
    return `<tr><td>${esc(r.k)}</td><td class="num muted">${r.n}</td>
      <td class="num">${eur(r.median)}</td>
      <td class="num muted">${med&&Math.abs(d)>=1?(d>0?"+":"")+d+"%":""}</td></tr>`;
  }).join("")+`</tbody></table>`;
}
function groupStats(g){
  const f=g.fit;
  const baseline = f
    ? `<strong>Expected price = ${f.yr_pct==null?"a mileage":"a km + year"} model.</strong>
       Each 100,000 km ${pctSigned(f.km_pct)}${f.yr_pct==null
         ? " (this group is a single year, so year is not a predictor here)"
         : ", each model year "+pctSigned(f.yr_pct)}.
       Typically misses a car by ±${f.err}%, against ±${f.err_median}% for this group's
       median — which is why it is used here. R² ${f.r2}, fitted on ${f.n} cars with a
       mileage reading.`
    : `<strong>Expected price = the group median.</strong> Typically misses a car by
       ±${g.err_median}%. No km+year model: either too few cars carry a mileage reading,
       or the model lost to the plain median in cross-validation.`;
  return `<div class="note">${baseline}</div>
    <div class="mini">${miniTable("Median price by fuel",g.by_fuel,g.median)}
    ${miniTable("Median price by engine",g.by_engine,g.median)}</div>`;
}

/* ---------- cars (candidates) ---------- */
function drawCars(){
  const hide=document.getElementById("hideOdd").checked;
  const hideSus=document.getElementById("hideSus").checked;
  const rows=[]; let odd=0, sus=0;
  for(const c of DATA.cars){
    if(!match(c))continue;
    const g=GMAP[gkey(c)];
    if(!g && c[C.exp]==null)continue;
    const r=carRow(c,g);
    if(!(r.vs>0))continue;
    if(c[C.odd]){odd++; if(hide)continue;}
    if(c[C.flags]){sus++; if(hideSus)continue;}
    rows.push(r);
  }
  document.getElementById("oddCount").textContent = `(${odd.toLocaleString()})`;
  document.getElementById("susCount").textContent = `(${sus.toLocaleString()})`;
  paint("cars",rows,r=>`<tr class="click">
      <td class="main"><strong>${esc(cap(r.c[C.make]))} ${esc(mname(r.c[C.model]))}</strong>${
        r.g&&r.g.is_gen?`<span class="pill gen">${esc(r.g.gen)}</span>`:""}${
        oddPill(r.c)}${gonePill(r.c)}${flagPill(r.c)}${sitePill(r.c)}${alsoPill(r.c)}${r.drop?`<span class="pill">cut ${r.drop}%</span>`:""}
        <span class="s">${spec(r.c)||"&nbsp;"}</span></td>
      <td class="num">${r.year}</td>
      <td class="num"><strong>${eur(r.price)}</strong></td>
      <td class="num" title="${r.modelled?"what the model expects for this exact car":"group median"}">${
        eur(r.base)}${r.modelled?"":'<span class="pill">med</span>'}<span class="s">${
        r.lo?eur(r.lo)+"–"+eur(r.hi):"&nbsp;"}</span></td>
      ${vsCell(r.vslo)}${vsCell(r.vs)}
      <td class="num">${num(r.km)}${r.kmr?`<span class="s"${r.kmr>1.3?' style="color:var(--warn)"':""}>${r.kmr}× group</span>`:""}</td>
      <td>${alink(r.c[C.link])}</td></tr>${detailRow(r.c,8)}`,
    8,"no candidates match the filters");
}
wireExpand("cars");

/* ---------- price drops ---------- */
function drawDrops(){
  const rows=DATA.cars.filter(c=>c[C.drop]>0&&match(c)).map(c=>carRow(c,GMAP[gkey(c)]));
  paint("drops",rows,r=>`<tr class="click">
      <td class="main"><strong>${esc(cap(r.c[C.make]))} ${esc(mname(r.c[C.model]))}</strong>${
        gonePill(r.c)}${flagPill(r.c)}<span class="s">${spec(r.c)||"&nbsp;"}</span></td>
      <td class="num">${r.year}</td>
      <td class="num">${eur(r.first)}</td><td class="num"><strong>${eur(r.price)}</strong></td>
      <td class="num good">−${r.drop}%</td>
      <td class="num">${eur(r.base)}</td>
      <td class="num">${r.days ?? "—"}</td>
      <td>${alink(r.c[C.link])}</td></tr>${detailRow(r.c,8)}`,
    8,`No price cuts recorded yet &mdash; this fills in once you have scraped the
       same cars more than once.`);
}
wireExpand("drops");
wireSort("cars",()=>drawCars());
wireSort("drops",()=>drawDrops());

/* ---------- sold speed ---------- */
function drawSold(){
  const s=DATA.meta.sold;
  const box=document.getElementById("soldbox");
  if(!s.gone){
    box.innerHTML=`<div class="note">Nothing has disappeared yet. This view needs at
      least two scrapes a few days apart &mdash; then it shows how long cars stay listed,
      which is the only real feedback on whether your baselines are any good.</div>`;
    return;
  }
  if(!s.cheap_n && !s.dear_n){
    box.innerHTML=`<div class="tiles">
      <div class="tile"><div class="v">${s.gone}</div><div class="l">Listings gone</div></div>
      <div class="tile"><div class="v">${s.unmeasured}</div>
        <div class="l">of those, seen in only one scrape</div></div></div>
    <div class="note"><strong>${s.gone} listings vanished, but none of them can be timed yet.</strong>
      Every one was seen in a single scrape, so its first and last sighting are the same
      moment: the honest answer to &ldquo;how long was it listed?&rdquo; is
      <em>somewhere between zero and one scrape interval</em>, not zero.
      <br><br>Duration needs a listing observed at least twice. Scrape again in a few days
      &mdash; the ${s.gone} that just went are already counted, and the cars still up will
      start accumulating measurable listing time.</div>`;
    return;
  }
  const faster = s.cheap_days!=null && s.dear_days!=null && s.cheap_days<s.dear_days;
  box.innerHTML=`<div class="tiles">
    <div class="tile"><div class="v">${s.gone}</div><div class="l">Listings gone</div></div>
    <div class="tile"><div class="v">${s.cheap_days??"—"}</div>
      <div class="l">Median days listed &mdash; priced BELOW group median (${s.cheap_n})</div></div>
    <div class="tile"><div class="v">${s.dear_days??"—"}</div>
      <div class="l">Median days listed &mdash; priced ABOVE group median (${s.dear_n})</div></div>
  </div>
  <div class="note">${faster
    ? "Cheaper cars are disappearing faster than dearer ones — that is the market agreeing with your baselines."
    : "Cheaper cars are not yet disappearing faster. With little data that means nothing; if it holds after a few weeks, your groups are probably mixing different cars together."}
  <br><br>&ldquo;Gone&rdquo; means the listing stopped appearing while its model was still
  being scraped. Usually sold, sometimes just withdrawn. Gone listings are tagged
  <span class="pill gone">gone</span> in the tables above rather than hidden.</div>`;
}

function renderAll(){countMatches();drawGroups();drawDrill();drawCars();drawDrops();drawSold();}

/* ---------- chart ---------- */
const makeSel=document.getElementById("makeSel"),sel=document.getElementById("modelSel");
const tip=document.getElementById("tip");
const byMake={};
Object.keys(DATA.series).forEach(k=>{const i=k.indexOf("/");const mk=k.slice(0,i);
  (byMake[mk]=byMake[mk]||[]).push(k);});
makeSel.innerHTML=Object.keys(byMake).map(m=>`<option value="${esc(m)}">${esc(m)}</option>`).join("");
function fillModels(){sel.innerHTML=(byMake[makeSel.value]||[]).map(k=>
  `<option value="${esc(k)}">${esc(k.slice(k.indexOf("/")+1))} (${DATA.series[k].n})</option>`
  ).join("");}
makeSel.onchange=()=>{fillModels();drawChart();};
sel.onchange=drawChart;
let logTouched=false;
document.getElementById("logY").onchange=()=>{logTouched=true;drawChart();};
function drawChart(){
  const ser=DATA.series[sel.value], pts=ser?ser.pts:[];
  document.getElementById("chartTitle").innerHTML = !ser ? "" :
    `${esc(sel.value)} · ${ser.n.toLocaleString()} cars · loses about
     <strong>${ser.dep==null?"—":ser.dep+"%"}</strong> of its value per model year`;
  const W=900,H=340,L=64,R=16,T=16,B=40,el=document.getElementById("chart");
  if(pts.length<2){el.innerHTML=`<text x="20" y="30" fill="var(--text-muted)">not enough data</text>`;return;}
  const xs=pts.map(p=>p.year),lo=Math.min(...pts.map(p=>p.p25)),hi=Math.max(...pts.map(p=>p.p75));
  const xmin=Math.min(...xs),xspan=(Math.max(...xs)-xmin)||1;
  const px=year=>L+(year-xmin)/xspan*(W-L-R);
  /* Depreciation is multiplicative, so a wide-range series is unreadable on a
     linear axis — a6 spans €599 to €55,550 and flattens 1998-2012 onto the axis.
     Default to log whenever the range is wide enough for that to bite. */
  const logBox=document.getElementById("logY"), logOK=lo>0&&hi/lo>8;
  if(!logTouched) logBox.checked=logOK;      /* until the reader overrides it */
  logBox.disabled=!(lo>0);
  const log=logBox.checked&&lo>0;
  const t=v=>log?Math.log(v):v, tlo=t(lo), tspan=(t(hi)-tlo)||1;
  const py=v=>T+(1-(t(v)-tlo)/tspan)*(H-T-B);
  let s="<g>";
  [...Array(5)].map((_,i)=>log?Math.exp(tlo+tspan*i/4):lo+(hi-lo)*i/4).forEach(v=>{
    s+=`<line x1="${L}" x2="${W-R}" y1="${py(v)}" y2="${py(v)}" stroke="var(--border)" stroke-width="1"/>
        <text x="${L-10}" y="${py(v)+4}" text-anchor="end" font-size="11" fill="var(--text-muted)">${eur(v)}</text>`;});
  s+=`<path d="M${pts.map(p=>px(p.year)+","+py(p.p75)).join(" L")} L${
      pts.slice().reverse().map(p=>px(p.year)+","+py(p.p25)).join(" L")} Z" fill="var(--band)"/>`;
  s+=`<path d="M${pts.map(p=>px(p.year)+","+py(p.median)).join(" L")}" fill="none"
      stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  pts.forEach(p=>{s+=`<circle cx="${px(p.year)}" cy="${py(p.median)}" r="4.5" fill="var(--series-1)"
      stroke="var(--surface-2)" stroke-width="2" data-y="${p.year}" data-n="${p.n}" data-m="${p.median}"
      data-a="${p.p25}" data-b="${p.p75}" data-lo="${p.lo}" data-hi="${p.hi}"
      data-yoy="${p.yoy==null?"":p.yoy}"/>`;});
  const step=Math.ceil(pts.length/8);
  pts.forEach((p,i)=>{if(i%step===0||i===pts.length-1)
    s+=`<text x="${px(p.year)}" y="${H-B+18}" text-anchor="middle" font-size="11" fill="var(--text-muted)">${p.year}</text>`;});
  el.innerHTML=s+"</g>";
  el.querySelectorAll("circle").forEach(c=>{
    c.onmouseenter=e=>{const d=e.target.dataset;
      tip.innerHTML=`<strong>${d.y}</strong> &middot; ${d.n} cars<br>median <strong>${eur(+d.m)}</strong>
        ${d.yoy?` <span class="${+d.yoy<0?"":"flag"}">${+d.yoy>0?"+":""}${d.yoy}% vs ${+d.y-1}</span>`:""}
        <br><span class="muted">p25 ${eur(+d.a)} &middot; p75 ${eur(+d.b)}</span>
        <br><span class="muted">cheapest ${eur(+d.lo)} &middot; dearest ${eur(+d.hi)}</span>`;
      tip.style.opacity=1;};
    c.onmousemove=e=>{tip.style.left=(e.clientX+14)+"px";tip.style.top=(e.clientY-10)+"px";};
    c.onmouseleave=()=>tip.style.opacity=0;});
}
fillModels();drawChart();renderAll();
</script></body></html>"""


def main():
    # The report is ASCII-safe, but this console output is not, and a Windows
    # console defaults to cp1252 where the separators come out as "?".
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    ap = argparse.ArgumentParser(description="Price baselines from the scraped database")
    ap.add_argument("--db", default=str(project_path(SETTINGS["paths"]["database"])))
    ap.add_argument("-o", "--output", default=str(REPORTS_DIR / "report.html"))
    ap.add_argument("--generations", default=str(DATA_DIR / "generations.json"))
    ap.add_argument("--bucket", type=int, default=CFG["bucket"], help="year band width within a generation")
    ap.add_argument("--min-n", type=int, default=CFG["min_n"], help="minimum cars per group")
    ap.add_argument("--min-price", type=int, default=DEF_MIN_PRICE,
                    help="ignore listings below this (parts cars, typos)")
    ap.add_argument("--max-price", type=int, default=DEF_MAX_PRICE)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--no-pooled", action="store_true", default=not CFG["pooled"],
                    help="skip the pooled LightGBM model; use per-group baselines only")
    ap.add_argument("--candidates", type=int, default=CFG["candidates"],
                    help="how many unverified below-range cars to list for "
                         "description fetching (default 300)")
    ap.add_argument("--candidates-file", default=str(DATA_DIR / "candidates.txt"))
    args = ap.parse_args()

    bucket = max(1, args.bucket)
    min_n = max(1, args.min_n)

    Store(args.db).close()            # creates or upgrades the database layout
    compiled = compile_generations(ensure_generations(args.generations))
    cars, skipped = load(args.db, compiled, args.min_price, args.max_price)
    if not cars:
        sys.exit("No usable listings — scrape some data first.")
    cars = assign_groups(cars, bucket, min_n)
    cars = mark_disappeared(cars, model_scrape_times(args.db))
    cars, duplicates = merge_duplicates(cars, args.db)
    groups = build_groups(cars, min_n)
    series = model_series(cars, max(3, min_n))
    sold = sold_summary(cars, groups)

    pooled = None
    if not args.no_pooled:
        print(f"Fitting the pooled price model on {len(cars):,} cars "
              f"({len(POOLED_QUANTILES)} quantiles x 5 folds)...")
        pooled = fit_pooled(cars, save_dir=DATA_DIR / "price_model")
        if pooled is None:
            print("  lightgbm is not installed - using per-group baselines. "
                  "pip install lightgbm numpy", file=sys.stderr)

    cols = ["make", "model", "grp", "year", "price", "km", "fuel", "engine",
            "link", "kmr", "drop", "first", "days", "gone", "exp", "odd",
            "description", "description_checked",
            "lo", "hi", "kw", "gear", "body", "flags", "loc", "dealer", "src", "also"]
    prefix = "https://autoplius.lt/skelbimai/"
    packed = []
    for c in cars:
        g = groups.get((c["make"], c["model"], c["grp"]))
        kmr = round(c["mileage_km"] / g["median_km"], 2) \
            if (g and g["median_km"] and c["mileage_km"]) else None
        if pooled:
            exp, lo, hi = c["p_mid"], c["p_lo"], c["p_hi"]
        else:
            exp = expected_price(g["fit"], c["mileage_km"], c["year"]) if g else None
            lo = hi = None
        c["exp"], c["lo"] = exp, lo
        packed.append([
            c["make"], c["model"], c["grp"], c["year"], c["price_eur"],
            c["mileage_km"], c["fuel"] or "", c["engine_l"] or "",
            (c["link"] or "").removeprefix(prefix), kmr, c["drop_pct"],
            c["first_price"], c["days"], 1 if c["gone"] else 0,
            round(exp) if exp else None,
            1 if c.get("price_outlier") else 0,
            c.get("description") or "",
            1 if c.get("description_checked_at") else 0,
            round(lo) if lo else None, round(hi) if hi else None,
            c.get("power_kw"), c.get("gearbox") or "", c.get("body") or "",
            "|".join(c["flags"]), c.get("location") or "", c.get("dealer"), c["source"],
            # the same car elsewhere: "site|price|link", several joined by "~"
            "~".join(f"{o['source']}|{o['price_eur']}|{o['link']}" for o in c.get("also", []))])

    # Cheapest-looking cars nobody has read yet: these are what the targeted
    # description fetch (scrape.py --ids-file) should spend requests on.
    # Already-flagged cars are left out — they have their answer.
    def under(c):
        base = c["lo"] or c["exp"]
        return (base - c["price_eur"]) / base if base else None
    unread = [c for c in cars if not c["suspect"] and not c.get("price_outlier")
              and not c.get("description_checked_at") and (under(c) or 0) > 0]
    unread.sort(key=under, reverse=True)
    cand_path = Path(args.candidates_file)
    cand_path.parent.mkdir(parents=True, exist_ok=True)
    with cand_path.open("w", encoding="utf-8") as fh:
        fh.write("# id  price  expected-low  (best first) - written by autoplius_stats.py\n")
        for c in unread[:args.candidates]:
            fh.write(f"{c['source']}:{c['id']}  {c['price_eur']}  {round(c['lo'] or c['exp'])}  "
                     f"{c['make']}/{c['model']} {c['year']}\n")

    bad_mileage = skipped.pop("impossible mileage", 0)
    skipped_total = sum(skipped.values())

    glist = sorted(groups.values(), key=lambda g: -g["n"])
    fitted = [g for g in glist if g["fit"]]
    errs = [g["err"] for g in glist]     # whichever baseline each group actually uses
    payload = {
        "cols": cols, "cars": packed, "groups": glist, "series": series,
        "meta": {
            "cars": len(cars), "groups": len(glist),
            "gen_groups": sum(1 for g in glist if g["is_gen"]),
            "models": len({(c["make"], c["model"]) for c in cars}),
            "median": round(statistics.median([c["price_eur"] for c in cars])),
            "drops": sum(1 for c in cars if c["drop_pct"] > 0),
            "min_n": min_n, "sold": sold, "duplicates": duplicates,
            "fitted": len(fitted),
            "baseline_err": round(statistics.median(errs), 1) if errs else None,
            "median_err": round(statistics.median([g["err_median"] for g in glist]), 1) if glist else None,
            "priced": sum(1 for c in packed if c[cols.index("exp")] is not None),
            "skipped_total": skipped_total, "skipped": skipped,
            "bad_mileage": bad_mileage,
            "pooled": pooled,
            "suspect": sum(1 for c in cars if c["suspect"]),
            "declared": sum(1 for c in cars if c["suspect_declared"]),
            "damage_checked": sum(1 for c in cars if c.get("damaged") is not None),
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        },
    }
    # "</" would close the <script> block early and take the rest of the page with
    # it; "<\/" is the same string to a JS parser. ensure_ascii also neutralises
    # U+2028/U+2029, which are newlines in JS but not in JSON.
    data_js = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).replace("</", "<\\/")
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(HTML.replace("__DATA__", data_js), encoding="utf-8")

    m = payload["meta"]
    detail = ", ".join(f"{v} {k}" for k, v in sorted(skipped.items(), key=lambda kv: -kv[1]))
    print(f"{m['cars']:,} priced listings"
          + (f" · {skipped_total} skipped ({detail})" if skipped_total else ""))
    if bad_mileage:
        print(f"{bad_mileage} mileage readings discarded as impossible "
              f"(> {MAX_KM:,} km or > {MAX_KM_PER_YEAR:,} km/year)")
    print(f"{m['models']} models · {m['groups']} groups of {min_n}+ "
          f"({m['gen_groups']} by generation + year band, rest by year band)")
    print(f"Overall median EUR {m['median']:,} · {m['drops']} price cuts · "
          f"{sold['gone']} listings gone")
    km_only = sum(1 for g in fitted if g["fit"]["yr_pct"] is None)
    shape = f"{m['fitted']}/{m['groups']} groups earned a price model"
    if km_only:
        shape += f" ({km_only} on mileage alone, single-year bands)"
    print(f"{shape}; {m['priced']:,} cars get an expected price. Typical baseline error "
          f"±{m['baseline_err']}% vs ±{m['median_err']}% median-only")
    if pooled:
        print(f"Pooled model: ±{pooled['err']}% median error out of fold, "
              f"{pooled['coverage']}% of clean cars inside their p10-p90 range "
              f"(80% expected)")
        if pooled.get("saved"):
            print(f"  saved for scoring new ads: {pooled['saved']}")
        print("  feature coverage: " + ", ".join(
            f"{k} {v / pooled['n']:.0%}" for k, v in pooled["filled"].items()))
    if duplicates:
        print(f"{duplicates:,} cars are listed on two sites; each is counted once")
    print(f"{m['suspect']:,} cars flagged ({m['declared']:,} declared defects; "
          f"damage search has covered {m['damage_checked']:,} cars)")
    print(f"{min(len(unread), args.candidates)} unread candidates written to {cand_path}\n")
    print("Biggest groups:")
    for g in glist[:12]:
        name = f"{g['make']}/{g['model']}"
        tag = "gen" if g["is_gen"] else "yrs"
        fit = g["fit"]
        if not fit:
            basis = f"median ±{g['err_median']}%"
        else:
            slopes = f"100k km {fit['km_pct']:+.0f}%"
            if fit["yr_pct"] is not None:
                slopes += f", yr {fit['yr_pct']:+.0f}%"
            basis = f"fit ±{fit['err']}% ({slopes})"
        print(f"  {g['n']:5d}  {name:<22} {g['gen'] or '-':<6} {g['band']:<10} [{tag}]"
              f"  median EUR {g['median']:>7,}  spread {g['spread']:>3}%  {basis}")
    size = out.stat().st_size / 1e6
    print(f"\nReport written to {out.resolve()}  ({size:.1f} MB)")
    if size > 25:
        print("  Large file — use --min-n to trim, or scrape fewer models.", file=sys.stderr)
    if args.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
