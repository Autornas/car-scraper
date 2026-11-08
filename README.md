# Used-car scraper and price reports (autoplius.lt + autogidas.lt)

Python tools that collect public used-car listings from autoplius.lt and
autogidas.lt into one database, catch new ads within minutes, and tell
bargains apart from damaged or problem cars.

## Quick start

1. **Install** (once, from the project root in PowerShell):

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

2. **Start the web interface:**

   ```powershell
   .\.venv\Scripts\python.exe scripts\scraper_dashboard.py
   ```


   It opens <http://127.0.0.1:8765/admin> in your browser and starts the
   five-minute watcher. Keep the terminal open; Ctrl+C stops everything.
   Add `--no-watcher` to start the site without the watcher.

3. **Fill the database.** On the first run it is empty. In the dashboard's
   **Control** section press **Resume scraping** (or run
   `python scripts\scrape.py` in a second terminal). A full first pass takes
   several nights because both sites rate-limit long runs; the nightly job
   (see "Nightly refresh") carries on where it stopped.

4. **Build the report.** Press **Rebuild report** (or run
   `python scripts\autoplius_stats.py`). This fits the price model and writes
   the deals report. Then open **Deals & prices** to browse it.

5. **Optional:** schedule the nightly refresh and set up Telegram alerts
   (both described below).

## How everything works

```
autoplius.lt ─┐                          ┌─ watcher (every 5 min): new ads, priced at once
              ├─ collectors ─ database ──┼─ nightly job: re-scrape stale models, damage sweep, rebuild
autogidas.lt ─┘   (scripts/     (data/   └─ report + price model ─ website (/admin, /)
                  collectors/)  autoplius.db)
```

1. **Collect.** `scripts/scrape.py` walks each site model by model (autoplius)
   or make by make (autogidas) and stores every listing, its price history and
   its card details (kW, gearbox, body, town, dealer) in `data/autoplius.db`.
   A site-wide damage sweep marks cars with a declared defect.
2. **Catch new ads.** `scripts/watch.py` reads each site's newest-first list
   every 5 minutes and stores anything it hasn't seen. New ads are priced
   straight away; a cheap one has its ad opened and gets a verdict.
3. **Combine sites.** The same car posted on both sites is matched and counted
   once, shown as one row with a link to the other listing.
4. **Price.** `scripts/autoplius_stats.py` fits one LightGBM model over all
   cars and gives each an expected price with a p10-p90 range. Cars below the
   range are candidates for a deal.
5. **Judge.** Each candidate gets a verdict: *deal*, *unchecked*, *damaged*,
   *problem* or *not a real price*. Ads of the best unchecked candidates are
   read for problem words. You can correct any verdict with a label, and the
   report tracks how often you agreed.
6. **Show.** The website serves the report and the control dashboard. Admins
   (you) start/stop tasks, edit the model list and label cars at `/admin`;
   viewers get a read-only copy of the deals at `/`. Telegram can message you
   about new deals.
7. **Repeat.** The nightly job keeps the data fresh without long bursts of
   requests, and the report rebuilds automatically when the database changes.

Where things live:

| Path | What |
|---|---|
| `config.toml` | all settings: paths, delays, enabled sites, web and alert options |
| `data/autoplius.db` | the database (listings, price history, labels, duplicates, audit log) |
| `data/price_model/` | the saved price model the watcher uses |
| `data/secrets.toml` | Telegram token (kept out of git) |
| `data/logs/` | nightly job logs |
| `reports/` | the generated report |
| `scripts/` | the tools; `scripts/collectors/` per-site code, `scripts/web/` the website |

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
  (full scrape per make, damage sweep, watcher, ad pages); same-car matching
  across sites; verdicts and your labels; FastAPI website with admin and
  viewer roles, audit log and Telegram alerts.
- **In progress:** the first full pass over both sites. Both sites
  rate-limit long runs, so the nightly job finishes it a few hundred models a
  night.
- **Not started:** putting the site online (Cloudflare Tunnel), backups,
  monitoring.

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
- [x] **7. Web server:** FastAPI site with admin and viewer roles, Cloudflare
  Access sign-in, watcher alongside tasks, audit log, Telegram alerts.
- [ ] **8. Go live:** Cloudflare Tunnel + Access, Windows service, backups,
  monitoring.

## Configuration

Default settings live in [config.toml](config.toml): paths, request delays,
watcher interval, report options and which sites are enabled. Command-line
flags still override them for a single run.

## Setup

From the project root:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

To also run the tests, install the development requirements instead (they
include `requirements.txt` plus pytest and pyflakes):

```powershell
python -m pip install -r requirements-dev.txt
```

## Tests

Parsers are tested against real pages saved in `tests/fixtures/`. When a site
changes its layout, these tests fail instead of the scraper quietly saving
wrong data. With the virtual environment active and the development
requirements installed:

```powershell
python -m pytest
```

or without activating it: `.\.venv\Scripts\python.exe -m pytest`. If that
says `No module named pytest`, the development requirements are missing.

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
| **not a real price** | under 30% of expected, or under 50% and far outside its group: lease figure, deposit, typo, parts car |

The Deals tab filters by verdict; the default hides damaged, problem and
fake prices. Open a car and mark it **real deal**, **damaged** or **fake
price**; the New deals list on the dashboard has the same buttons. Labels are
kept in the `labels` table, override the verdict in the list, and the
**Verdicts & sold speed** tab shows how often you agreed with each verdict and
how fast each verdict's cars disappear. Saving labels works when the report is
opened from the dashboard.

**Current cars only.** Sold cars, and cars no scrape has seen for over
`report.stale_days` (10) days, are hidden from the Deals list unless you tick
*Include cars not seen recently*, and the nightly job doesn't spend ad reads
on them. An ad page that answers "not found" marks its car as sold.

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

## Website and control dashboard

From the project root in PowerShell, start the site:

```powershell
.\.venv\Scripts\python.exe scripts\scraper_dashboard.py
```

It opens `http://127.0.0.1:8765/admin` and starts the five-minute watcher.
It only ever listens on this PC (127.0.0.1). Keep its terminal open; Ctrl+C
stops the site, the watcher and any running task.

| Page | Who | What |
|---|---|---|
| `/` | everyone signed in | the deals report, viewer copy |
| `/admin` | admins | the control dashboard (below) |
| `/report` | everyone | admins get the full report, viewers the viewer copy |

**Sign-in** (`[web]` in `config.toml`):

- `auth = "local"` (default): only you, on this PC, as admin. A request that
  reaches the site through any other hostname (a tunnel) is refused, so this
  mode can't be exposed by accident.
- `auth = "tailscale"`: reach the site from your own devices over Tailscale
  (free, no domain), see "Remote access with Tailscale" below. Only requests
  through your `*.ts.net` name that carry Tailscale's signed-in login are
  served; the login must be in `admins` or `viewers`.
- `auth = "cloudflare"`: for use behind Cloudflare Tunnel + Access. Every
  request must carry Cloudflare's signed login token; it is verified against
  your team's keys (`team_domain`), the application's AUD tag (`audience`),
  the issuer and the expiry. The email in it must be in `admins` or
  `viewers`; anyone else is refused. Setting up the tunnel is Phase 8.

**Remote access with Tailscale** (watch the scrapers from another PC without
Remote Desktop):

1. Install Tailscale on the scraper PC and on each PC/phone you'll watch from,
   and sign in to the same account (`winget install Tailscale.Tailscale`).
2. Create `config.local.toml` (git ignores it; it overrides `config.toml` for
   this PC only) with `[web]`, `auth = "tailscale"` and
   `admins = ["you@gmail.com"]` (the email you sign in to Tailscale with).
3. Auto-start the site at sign-in:
   `powershell -ExecutionPolicy Bypass -File scripts\install_dashboard_task.ps1`
4. On the scraper PC: `tailscale serve --bg 8765`. It prints your address,
   `https://<pc>.<tailnet>.ts.net`. Open `/admin` there from any of your devices.

**Auto-update from GitHub** (run the PC like a server): push your changes, and
within five minutes this PC pulls them and restarts the site if code changed.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_autoupdate_task.ps1
```

It only fast-forwards, skips if tracked files have local edits, rolls back if
the new version doesn't start, and logs to `data\logs\autoupdate.log`. A restart
stops a running scrape (the watcher restarts itself). Remove it with `-Remove`.
Keep PC-specific settings in `config.local.toml`, not `config.toml`.

Don't use `tailscale funnel`: it makes the site public, and the site would
refuse everyone anyway, because funnel requests carry no login.

**Viewers** get a copy of the report with your labels removed (and sellers'
descriptions too if `viewer_descriptions = false`), served compressed (~2 MB).
They can't open `/admin` or call any admin API: the server checks the role
on every request, whatever the page shows.

**Changes** (start/stop, labels, model list) must be JSON sent from the
site's own pages (Origin = Host), and are written to an audit log shown on
the Control page. Every answer carries a strict Content-Security-Policy and
the usual security headers.

**Telegram alerts** (optional): create a bot with @BotFather, then put its
token and your chat id in `data/secrets.toml` (kept out of git):

```toml
[telegram]
bot_token = "123456:ABC..."
chat_id = "123456789"
```

The watcher then messages you for every new car it rates **deal** and when a
site's watcher fails 3 scans in a row (`[alerts]` in `config.toml`).

The control dashboard at `/admin` has three sections:

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
The watcher runs in its own slot, alongside one task at a time; the output
panel switches between the task and the watcher. Normal completion of a task
is shown as a success notice;
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
