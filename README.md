# Used-car scraper and price reports (autoplius.lt + autogidas.lt)

Python tools that collect public used-car listings from autoplius.lt and
autogidas.lt into one database, catch new ads within minutes, and tell
bargains apart from damaged or problem cars.

## Project plan

**Goal:** one PC collects every used-car listing from autoplius.lt and
autogidas.lt into one database. It catches new ads within minutes, separates
real bargains from damaged / problem cars, and serves a website where admins
control the scrapers and users browse deals. The full plan, with the reasoning
and the site details behind it, is in [ROADMAP.md](ROADMAP.md).

### Status
- **Done:** Autoplius full scraper with price history; card details (kW,
  gearbox, body, town, dealer); damage detection (site filter, ad page,
  problem words); description reading for top candidates only; pooled
  LightGBM price model with p10–p90 ranges; report; local dashboard;
  gap-free 5-minute watcher that prices new ads and checks cheap ones; full
  make/model catalogue with id-based searches; site-wide damage sweep;
  multi-site database and shared collector code; autogidas.lt collector
  (full scrape per make, damage sweep, watcher, ad pages).
- **In progress:** the first full pass over both sites. Both sites
  rate-limit long runs, so the nightly job finishes it a few hundred models a
  night.
- **Not started:** multi-user website, hosting, alerts.

### Phases
- [x] **0. Clean foundation:** git, archive old files, `config.toml`, parser
  tests on saved pages.
- [x] **1. Gap-free watcher:** newest-first sort, walk until a whole page is
  already known, write straight to the database, score new ads, health
  tracking.
- [ ] **2. Complete Autoplius data:** site-wide damage sweep, full re-scrape,
  explain the 37k vs 46.5k gap, nightly refresh.
- [x] **3. Multi-site database:** every row keyed by (source, id), make/model
  aliases per site, shared collector code in `scripts/collectors/`.
- [x] **4. autogidas.lt collector:** parsers, full scrape per make, damage
  sweep (`f_46`), watcher (`f_50=naujausi_desc`), ad pages.
- [x] **5. Combine sites:** the same car on both sites is matched, counted
  once, and shown as one row with a link to the other listing.
- [x] **6. Verdicts:** deal / unchecked / damaged / problem / not a real
  price on every cheap car, your own labels from the report and dashboard,
  agreement and sold-speed per verdict.
- [ ] **7. Web server:** FastAPI, admin and viewer roles, paginated data,
  alerts.
- [ ] **8. Go live:** Cloudflare Tunnel + Access, Windows service, backups,
  monitoring.

## Configuration

Default settings live in [config.toml](config.toml): paths, request delays,
watcher interval, report options and which sites are enabled. Command-line
flags still override them for a single run.

## Tests

Parsers are tested against real pages saved in `tests/fixtures/`. When a site
changes its layout, these tests fail instead of the scraper quietly saving
wrong data.

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## Setup

From the project root:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Tools

- `scripts/scrape.py` builds and refreshes the database, site by site:
  catalogue sync, model-by-model scrape, damage sweep, ad pages, coverage.
- `scripts/watch.py` catches every new ad within minutes and writes it
  straight into the database (see "New-listing watcher").
- `scripts/autoplius_stats.py` reads the database and writes
  `reports/report.html` and the saved price model.
- `scripts/scraper_dashboard.py` is the local web interface for all of it.
- `scripts/nightly.py` is the unattended nightly refresh.

Both scrapers run per site (`--site autoplius`; by default every site enabled
in `config.toml`). The code behind them lives in `scripts/collectors/`:

| Module | What it holds |
|---|---|
| `common.py` | polite fetching (waits out rate limits), refusal detection, name helpers |
| `store.py` | the database: listings, price history and models keyed by (source, id); per-site make/model aliases; the automatic upgrade of older files |
| `crawl.py` | the model-by-model scrape, price-band splitting, damage sweep, ad pages, coverage |
| `watch.py` | the gap-free page walk, title matching, verdicts, health |
| `autoplius.py` | everything specific to autoplius.lt: URLs, card and ad parsers, catalogue, damage filter |
| `autogidas.py` | the same for autogidas.lt |

A new site is one more module like these, registered in
`collectors/__init__.py` and switched on in `config.toml`.

**Shared make/model names.** Autoplius' catalogue is the canonical list. Cars
from other sites are filed under it by matching their card titles against it
("Mercedes-Benz C 220 T CDI" -> `mercedes-benz/c220`), so the price model
treats a car the same whichever site it came from. `model_aliases` is there
for a site's own names that need mapping by hand.

### Verdicts and your labels

Every car priced under what the model expects gets one verdict
(`scripts/collectors/verdicts.py`, the same rules in the report and the
watcher):

| Verdict | Meaning |
|---|---|
| **deal** | under the low end of its range (p10), ad read, nothing found |
| **unchecked** | under the low end, ad not read yet |
| **damaged** | a defect declared on the site |
| **problem** | problem words in the seller's text (not running, no TA, export, parts, lease transfer) |
| **not a real price** | under 30% of expected or far outside its group: lease figure, deposit, typo, parts car |

The Deals tab filters by verdict; the default hides damaged, problem and
fake prices. Open a car and mark it **real deal**, **damaged** or **fake
price**; the New deals list on the dashboard has the same buttons. Labels are
kept in the `labels` table, override the verdict in the list, and the
**Verdicts & sold speed** tab shows how often you agreed with each verdict and
how fast each verdict's cars disappear. Saving labels works when the report is
opened from the dashboard.

By definition about 10% of cars sit under their p10, so "unchecked" is
large. Its best (furthest under) cars are read first: the nightly job reads
`report.nightly_reads` (150) ads per site, and **Check top candidates** in the
dashboard does it on demand. The site-wide damage sweep, which runs after
each complete scrape, turns many of them into **damaged** directly.

### The same car on two sites

Many sellers post a car on both sites (about 1,200 cars so far, ~13% of
autogidas). `scripts/collectors/dupes.py` pairs them by the car itself: make,
model and year must agree, mileage within 2%, and nothing may contradict
(fuel, gearbox, engine, kW). A score then weighs identical mileage (less if
it's a round number), town, kW, engine, price and dealer; a different town or
a price more than 35% apart counts against. Each listing joins at most one
pair, best first.

- **Report:** one row per car. The cheaper listing is shown, with an
  "also on autogidas €12,900" link to the other; the hidden one stays out of
  the group medians and the price model's training.
- **Watcher:** a new deal is checked against the other site, and the
  dashboard shows "also on …" with that listing's price.
- **Price model:** the site is now one of its inputs, in case one site's
  asking prices run systematically higher.

Pairs are rebuilt on every report build and kept in the `duplicates` table.

### autogidas.lt

- **Per make, not per model.** Its full model lists come only from `/ajax`
  and `/select` endpoints, which its robots.txt disallows, so the scraper
  searches each make (the make list with counts is on every results page,
  ~90 makes) and reads each car's model from its title. A full pass is
  ~1,400 pages; big makes split by price as on Autoplius.
- **Damage sweep:** one search per defect type (`f_46`): damaged, burnt,
  flooded, hail, engine, gearbox, other major (~750 cars). "Other minor
  defects" is left out, as Autoplius' filter leaves it out.
- **Watcher:** the same gap-free walk over its newest-first list.
- **Card details** match Autoplius' spellings (fuel, gearbox, body), and the
  body type is taken from the ad URL since cards don't show it.

Settings are in the `[autogidas]` section of `config.toml`;
`enabled = false` there switches the site off everywhere.

**Database upgrade.** The first run of the new code converts an older
single-site database automatically and keeps the old file next to it as
`autoplius.db.pre-v2-<date>`.

**Pace.** One request at a time, 2 s apart (`[autoplius]` in `config.toml`).
A faster pass (three parallel requests, 1 s apart) got HTTP 429 and then a
CAPTCHA after ~350 models. A "too many requests" answer is now waited out
(30 s, 90 s, 240 s, or the site's own `Retry-After`) before the run gives up,
and a challenge page is never mistaken for an empty model.

## Scraper control dashboard

From the project root in PowerShell, start the dashboard:

```powershell
.\.venv\Scripts\python.exe scripts\scraper_dashboard.py
```

It opens `http://127.0.0.1:8765/` and starts the five-minute watcher
automatically. The dashboard is local to this computer. Keep its terminal
open; Ctrl+C stops the dashboard and active task. It has three sections:

- **Control**: database numbers (listings, models, damage coverage, kW
  coverage, descriptions read, candidates waiting, new ads in 24 h, report
  age), the task list with the watcher's health, the **New deals** list, and
  the live output of the running task. Tasks: watcher, resume scraping,
  full re-scrape, flag damaged cars, check top candidates (rebuild → read the
  candidate ads → rebuild), and rebuild report. Only one task runs at a time;
  the running task shows in the top bar with a Stop button from any section.
- **Deals & prices**: the report, filling the window. Header, tabs and filters
  stay put and each table scrolls inside its own box, so nothing needs
  scrolling sideways. Click a car row for its details and seller description.
  Groups show their cars next to them (stacked on narrower screens). The
  report is rebuilt automatically if the database changed; **Reload report**
  picks up new data.
- **Models**: which of the site's models the database scraper covers. It
  starts as every make and model on Autoplius; remove a `make/model` line to
  switch that model off (its saved listings stay), add it back to switch it
  on. Only names from the site's catalogue are accepted.

The dashboard displays a dismissible pop-up if a scraper reports common access
or runtime errors, exits unsuccessfully, or if the watcher stops unexpectedly.
Normal completion of the database scraper is shown as a success notice;
stopping a task from the dashboard is shown separately.
When a scraper gets HTTP 403 from Autoplius, the dashboard opens that refused
page in your default browser once per run and shows a link to it. You can
inspect or complete a challenge manually if the site presents one. Browser
verification is not transferred to the scraper, so it may still be necessary
to wait before resuming.

The database scraper stores listings, seller descriptions, and price history
in `data/autoplius.db`. From each result card it also saves power (kW),
gearbox, body type, town and whether the seller is a dealer.

**Which models.** The scraper covers Autoplius' whole make/model catalogue
(~3,200 models across ~200 makes), read from one page of the site and synced
automatically once a week (`--refresh-models` forces it). Every search goes by
the site's own `make_id` / `model_id`, never by URL names: the old name-based
URLs for makes with a hyphen (`mercedes-benz`, `land-rover`) and models like
`ct-200h` had quietly started returning the whole site, which is why Mercedes
was almost missing. Page 1 of each search is read first and its result count
decides how many more pages to fetch, so a small model costs one request.
Models are re-scraped after 7 days (`autoplius.max_age_days`); models with no
ads only every 30 days (`empty_max_age_days`).

**Damage sweep.** After scraping, one site-wide search with Autoplius' own
"Defektai" filter (damaged, burnt, flooded, hail, engine or gearbox defect,
other major defects) reads every car with a declared defect, about 2,300 cars
on ~120 pages, and marks them `damaged = 1`, every other listing `0`. To run
only the sweep:

```powershell
.\.venv\Scripts\python.exe scripts\scrape.py --damage-sweep
```

**Coverage.** `--coverage` compares the site's live per-make counts with the
database and lists the biggest gaps:

```powershell
.\.venv\Scripts\python.exe scripts\scrape.py --coverage
```

**Nightly refresh.** `scripts/nightly.py` re-scrapes up to
`autoplius.nightly_limit` (500) of the stalest due models, runs the damage
sweep and rebuilds the report and price model (a running watcher picks the new
model up). 500 models a night covers the whole catalogue about once a week
without one long burst of requests. To have Windows run it every night at 03:30:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_nightly_task.ps1
```

`-At 04:00` changes the time and `-Remove` deletes the task. It runs while you
are signed in and catches up at the next sign-in if the PC was off. Logs go to
`data/logs/`.

Descriptions are no longer fetched for every listing after a scrape (one
request per ad, tens of hours for a full database). They are fetched for the
best-looking candidates instead:

```powershell
.\.venv\Scripts\python.exe scripts\autoplius_stats.py
.\.venv\Scripts\python.exe scripts\scrape.py --backfill-descriptions --ids-file data\candidates.txt
.\.venv\Scripts\python.exe scripts\autoplius_stats.py --open
```

`autoplius_stats.py` writes `data/candidates.txt`: the cheapest unflagged,
unread cars relative to their expected price, best first (`--candidates N`,
default 300). Fetching an ad also reads its parameter table (kW, gearbox,
body, declared defect, first-registration country). Use `--all-descriptions`
on a scrape to restore the old fetch-everything behaviour.

## Price model and flags

With `lightgbm` installed, `autoplius_stats.py` fits one pooled model over every
car (make, model, generation, year, mileage, fuel, engine, kW, gearbox, body,
dealer, declared damage) instead of relying only on small per-group fits. Each
car is priced by a model that never saw it, and gets a median expected price
plus a p10–p90 range calibrated so 80% of clean cars fall inside it. The Cars
tab sorts by **vs low end**: positive means cheaper than 9 in 10 comparable
cars. Prices under 30% of expected are treated as outliers (lease payments,
typos, parts cars). `--no-pooled` falls back to the per-group baselines.

Cars are **flagged** (and hidden by default in the Cars tab) when Autoplius
lists a declared defect, or the title/description contains problem words:
damaged, not running, no TA, export, parts, lease transfer, no documents.
Negated phrases such as "be defektų" or "nedaužtas" don't count. The word
lists live in `FLAG_PATTERNS` in `scripts/autoplius_stats.py` and are
re-applied on every run.

Older databases are migrated automatically. kW, gearbox and body stay empty
until a model is scraped again (`scrape.py --force`), and the pooled
model gets sharper once they are filled.

To fetch every missing description (slow), run
`scrape.py --backfill-descriptions` without `--ids-file`. It fetches one ad at a time with a delay, saves progress as it goes, and stops
if Autoplius refuses a request or presents a browser challenge. No CAPTCHA or
other access check is bypassed.

## New-listing watcher

`scripts/watch.py` reads Autoplius' newest-first list
("Naujausi viršuje") every 5 minutes. Each scan keeps reading pages until a
whole page holds only ads already in the database, then one more page as a
margin. A quiet period costs two requests, a busy one reads further, and
after downtime the next scan walks back to where it left off (up to
`watcher.max_pages`, about a day of ads; "Resume scraping" fills anything
older). Raised and promoted ads in the list can't end a scan early, because
only a fully known page does.

New ads go straight into `data/autoplius.db` with every card field. Ads of
models missing from the model list are stored too, under a make/model taken
from the title. Each new ad is priced with the model `autoplius_stats.py`
saves to `data/price_model/`; a car under the low end of its range has its
ad opened at once and gets a verdict (deal, damaged, problem, not a real
price) in the dashboard's **New deals** list. Every scan is recorded, and the
dashboard shows when scans start failing.

```powershell
.\.venv\Scripts\python.exe scripts\watch.py            # every 5 minutes
.\.venv\Scripts\python.exe scripts\watch.py --once     # one scan
.\.venv\Scripts\python.exe scripts\watch.py --verify 6 # last 6 h all stored?
```

`--verify HOURS` walks back through the newest-first list and lists any ad
from that window that is not in the database. Settings live in the
`[watcher]` section of `config.toml`. To launch the dashboard without starting
the watcher, add `--no-watcher`.

Build the database and report with:

```powershell
python scripts/scrape.py               # everything due (first run: all ~3,200 models)
python scripts/autoplius_stats.py --open
```

Use `--help` on any tool for its options. Output paths can be changed with the
tools' path arguments. `data/generations.json` holds editable model-generation
boundaries. `scrape.py --models-file data/models.txt` scrapes only the
`make/model` pairs listed in that file.

Scrapes only pages publicly available to the request. Keep request delays
reasonable and follow Autoplius' terms.
