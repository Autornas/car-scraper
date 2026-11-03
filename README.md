# Autoplius scraper and price reports

Small Python command-line tools for collecting public Autoplius car listings
and generating local price-analysis reports.

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
  LightGBM price model with p10–p90 ranges; report; local dashboard.
- **Built, not yet run on the data:** full re-scrape for kW / gearbox / body,
  damage sweep, candidate description check.
- **Broken:** the 5-minute watcher reads the "most relevant" sort (mostly
  promoted ads) from a fixed 3 pages, so it misses most new ads.
- **Not started:** autogidas.lt, multi-site database, multi-user website,
  hosting, alerts.

### Phases
- [x] **0. Clean foundation:** git, archive old files, `config.toml`, parser
  tests on saved pages.
- [ ] **1. Gap-free watcher:** newest-first sort, walk until a whole page is
  already known, write straight to the database, score new ads, health
  tracking.
- [ ] **2. Complete Autoplius data:** site-wide damage sweep, full re-scrape,
  explain the 37k vs 46.5k gap, nightly refresh.
- [ ] **3. Multi-site database:** `source` + `source_id`, shared make/model
  catalogue, shared collector code.
- [ ] **4. autogidas.lt collector:** parsers, full scrape, damage sweep
  (`f_46`), watcher (`f_50=naujausi_desc`).
- [ ] **5. Combine sites:** detect the same car on both, count it once.
- [ ] **6. Verdicts:** Deal / Damaged / Problem / Not a real price /
  Unchecked, with admin feedback.
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

- `scripts/autoplius_scraper.py` writes listing data to
  `data/autoplius_cars.csv`.
- `scripts/watch_new_listings.py` checks the latest result pages every five
  minutes and appends newly discovered ad IDs to `data/new_listings.csv`.
- `scripts/scraper_dashboard.py` provides a local web interface to start and
  stop the watcher and resumable database scraper, and view price breakdowns.
- `scripts/live_dashboard.py` is the watcher-only dashboard.
- `scripts/autoplius_db.py` stores resumable listings in `data/autoplius.db` and
checks listing titles against the selected make/model before saving, to avoid
classifying unrelated results under a model when the site returns a broad
search page.
- `scripts/autoplius_stats.py` reads that database and writes
  `reports/report.html`.

Run a small scrape with:

```powershell
python scripts/autoplius_scraper.py --pages 3
```

## Scraper control dashboard

From the project root in PowerShell, start the dashboard:

```powershell
.\.venv\Scripts\python.exe scripts\scraper_dashboard.py
```

It opens `http://127.0.0.1:8765/` and starts the five-minute watcher
automatically. The dashboard is local to this computer. Keep its terminal
open; Ctrl+C stops the dashboard and active task. It has three sections:

- **Control**: database numbers (listings, models, damage coverage, kW
  coverage, descriptions read, candidates waiting, report age), the task list,
  and the live output of the running task. Tasks: watcher, resume scraping,
  full re-scrape, flag damaged cars, check top candidates (rebuild → read the
  candidate ads → rebuild), and rebuild report. Only one task runs at a time;
  the running task shows in the top bar with a Stop button from any section.
- **Deals & prices**: the report, filling the window. Header, tabs and filters
  stay put and each table scrolls inside its own box, so nothing needs
  scrolling sideways. Click a car row for its details and seller description.
  Groups show their cars next to them (stacked on narrower screens). The
  report is rebuilt automatically if the database changed; **Reload report**
  picks up new data.
- **Models**: the model list the database scraper covers. Add or remove one
  `make/model` pair per line (for example `audi/a3`), then save. New entries
  are queued for scraping; removed ones stop being scraped but keep their
  saved listings and price history.

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
gearbox, body type, town and whether the seller is a dealer. After each model
it runs one extra search using Autoplius' own "Defektai" filter (damaged,
burnt, flooded, hail, engine/gearbox defect, other major defects) and marks
those listings `damaged = 1`, the rest of the model `damaged = 0`.

Descriptions are no longer fetched for every listing after a scrape (one
request per ad, tens of hours for a full database). They are fetched for the
best-looking candidates instead:

```powershell
.\.venv\Scripts\python.exe scripts\autoplius_stats.py
.\.venv\Scripts\python.exe scripts\autoplius_db.py --backfill-descriptions --ids-file data\candidates.txt
.\.venv\Scripts\python.exe scripts\autoplius_stats.py --open
```

`autoplius_stats.py` writes `data/candidates.txt`: the cheapest unflagged,
unread cars relative to their expected price, best first (`--candidates N`,
default 300). Fetching an ad also reads its parameter table (kW, gearbox,
body, declared defect, first-registration country). Use `--all-descriptions`
on a scrape to restore the old fetch-everything behaviour.

To flag damaged cars in an existing database without re-scraping every model:

```powershell
.\.venv\Scripts\python.exe scripts\autoplius_db.py --refresh-damage
```

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
until a model is scraped again (`autoplius_db.py --force`), and the pooled
model gets sharper once they are filled.

While the watcher runs, the dashboard also imports new watcher CSV rows when
their listing URL matches a known make/model; it leaves unmatched rows in the
CSV instead of guessing their model. It fetches descriptions for newly imported
watcher listings one at a time with a two-second pause.

To fetch every missing description (slow), run
`autoplius_db.py --backfill-descriptions` without `--ids-file`. It fetches one ad at a time with a delay, saves progress as it goes, and stops
if Autoplius refuses a request or presents a browser challenge. No CAPTCHA or
other access check is bypassed.

The first successful watcher scan establishes a baseline without exporting
existing listings. Later scans append newly observed IDs to
`data/new_listings.csv`. “New” means first observed on the watched result
pages, not a verified upload timestamp. The watcher remembers observed IDs
and scan history in `data/new_listings_seen.sqlite`.

To run only the watcher without a dashboard, use
`.\.venv\Scripts\python.exe scripts\watch_new_listings.py`. To launch the
scraper dashboard without starting the watcher automatically, add
`--no-watcher`.

Build the database and report with:

```powershell
python scripts/autoplius_db.py --limit 50
python scripts/autoplius_stats.py --open
```

Use `--help` on any tool for its options. Output paths can be changed with the
tools' path arguments. `data/generations.json` holds editable model-generation
boundaries. `data/models.txt` is an optional separate curated list for
`autoplius_db.py --models-file data/models.txt`; it is not the full scraper's
database inventory.

Scrapes only pages publicly available to the request. Keep request delays
reasonable and follow Autoplius' terms.
