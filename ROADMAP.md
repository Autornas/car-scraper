# Project roadmap

**Goal:** one PC collects every used-car listing from **autoplius.lt** and
**autogidas.lt** into one database. It catches new ads within minutes, tells
**real bargains** apart from **damaged / problem cars**, and serves a website.
**Admins** run and control the scrapers there; **users** browse deals and
prices.

Status as of 2026-11-03.

---

## 1. Where things stand

### Done and working
| Piece | Notes |
|---|---|
| Autoplius full scraper (`scripts/autoplius_db.py`) | Model by model from the site's model list, resumable, splits big models by price, SQLite with price history. |
| Card details: kW, gearbox, body, town, dealer | Parsed from result cards (code done, tested live). |
| Damage detection, Autoplius | Site's own "Defektai" filter + the ad page's defect field + problem words in title/description (with negations handled). |
| Targeted description reading | `--backfill-descriptions --ids-file data/candidates.txt` reads only the top unread candidates. |
| Pooled price model | LightGBM over all cars, priced out of fold, p10–p90 range per car. ±13.2% typical miss vs ±14.5% for the old group baselines. |
| Report | Deals list ranked by "vs low end", groups, price-by-year, price drops, sold speed. Fixed-height layout, no sideways scrolling. |
| Local dashboard | Control / Deals & prices / Models; six tasks, live output, one task at a time. |

### Built but not yet applied to your data
| Piece | What's missing |
|---|---|
| kW / gearbox / body in the database | Needs one full re-scrape (`Full re-scrape` task, ~2 h). Until then the price model can't use them. |
| Damage flags in the database | Needs `Flag damaged cars` or the re-scrape. |
| Descriptions for candidates | Needs `Check top candidates`. |

### Not working properly
| Piece | Problem |
|---|---|
| **5-minute watcher** | Reads the default sort, **"Aktualiausi viršuje"** (most relevant first), which is dominated by promoted and "raised" ads. In the scan history it kept seeing the same 60 listings and logged 0 new in most scans, so most new ads were missed. It also checks a fixed 3 pages (catches nothing after downtime), and 10 of its 20 recorded scans failed with nothing alerting you. It writes a CSV that the dashboard imports later by guessing the model from the URL, using an older parser without kW / gearbox / body. |
| **Coverage** | Autoplius shows **46,561** used cars; the database has **36,966**, last scraped 2026-10-28. Some of the gap is age; some is likely the model list or the title-match filter dropping ads. Not investigated yet. |
| **Housekeeping** | Not a git repository. Old copies of the scripts and 6 database backups sit in the project root, plus a stray `.py` file. |

### Not started
- autogidas.lt collection
- One combined schema for several sites, and cross-site duplicate detection
- Multi-user web server (admin / viewer roles), hosting, login
- Alerts (new deal found, watcher down)

---

## 2. What we know about the two sites

| | autoplius.lt | autogidas.lt |
|---|---|---|
| Used cars listed | ~46,500 | ~27,000 |
| Newest-first sort | `order_by=3&order_direction=DESC` ("Naujausi viršuje") | `f_50=naujausi_desc` |
| Results per page | 20 | 20 |
| Card fields | title, year-month, body, price, fuel, gearbox, engine l + kW, km, town, dealer logo, age badge ("Prieš 7 min.") | title, price, year, fuel, km, gearbox, engine L + kW, town, age badge, `data-price`, `data-updated` |
| Damage filter | `has_damaged_id[...]`, several values = OR | `f_46=<value>`, one value per search |
| robots.txt | — | Blocks `/paieska*`, `/ajax*`, `/mano-gidas*`, `/select*`; listing and ad pages allowed |
| New-ad rate (measured Monday ~17:00) | ~1 per minute (page 1 = last ~16 min, page 3 = ~75 min) | to measure |

The newest-first order follows publish time, not ad ID, so "stop at the
highest ID seen" won't work. The stop rule has to be "a whole page of ads
already known".

---

## 3. Target design

```
                     ┌──────────────── the scraping PC ────────────────┐
autoplius.lt ◄─────► │ collectors/autoplius ─┐                          │
                     │                       ├─► normalise ─► SQLite DB │
autogidas.lt ◄─────► │ collectors/autogidas ─┘                  │       │
                     │                                          ▼       │
                     │ scheduler: watchers every 5 min,   analysis:     │
                     │ nightly refresh, damage sweep,     price model,  │
                     │ model refit                        flags, dedupe │
                     │                                          │       │
                     │ web app (FastAPI) ◄──────────────────────┘       │
                     │   /admin  tasks, logs, settings, audit           │
                     │   /       deals, prices, search                  │
                     └──────────────┬───────────────────────────────────┘
                                    │ cloudflared (outbound only)
                              Cloudflare Tunnel + Access (login, 2FA)
                                    │
                         admin / users in any browser
```

Decisions:
- **SQLite stays.** One PC and a handful of users is well within what it
  handles. WAL mode, writes go through the collectors, the web app opens it
  read-only for viewers.
- **One listings table for both sites.** `source` + `source_id` is the
  unique key, and every field is normalised to the same names and units. A
  shared **make/model catalogue** maps each site's spelling to one name
  ("Mercedes-Benz" vs "mercedes", "320" vs "3 serija"). This is the most
  fiddly part of combining the sites, and the price model depends on it.
- **Collectors share one interface.** Each site gets a module that knows its
  URLs and HTML. Fetching, block detection, saving and the watcher logic are
  shared, so a third site later is just another module.
- **A scheduler instead of "one task at a time".** Each site's watcher runs
  every 5 minutes alongside long jobs. One job per site at a time keeps the
  request rate polite.
- **FastAPI web server** instead of the hand-written `http.server`: proper
  routing, a login check on every request, paginated JSON for the deals
  table, so users never download all cars at once (today's report is 8 MB).
- **Cloudflare Tunnel + Access** for hosting and login, with the app checking
  Cloudflare's signed login token on every request. Firebase Auth only if you
  later want public sign-up or more than 50 users.

---

## 4. Steps, in order

Each phase ends in something that works on its own. Sizes: **S** = an
evening, **M** = a day or two, **L** = several days.

### Phase 0 — Clean foundation (S)
**Status: done 2026-11-03.**
1. `git init`, first commit. Move old root scripts, database backups and
   `autoplius_old.db` into `archive/` (kept out of git).
2. One `config.toml`: data paths, delays, watcher interval, site switches.
3. Save a few real result and ad pages per site as **test fixtures**, with
   parser tests against them, so a site layout change shows up as a failing
   test instead of quietly wrong data.

**Done when:** tests pass, and the repo contains only current code.

### Phase 1 — Gap-free 5-minute watcher (M) ← biggest immediate win
**Status: done 2026-11-03.** All six points below are in
`scripts/watch_new_listings.py`; `--verify HOURS` is the completeness check.
1. Use the newest-first sort.
2. Walk pages until **a whole page is already known**, then one more page as
   a safety margin. Promoted ads that reappear never count as the stop
   signal. No fixed page limit, only a safety cap of about 50 pages.
3. After downtime (PC off, site refusing requests) it walks back to the last
   ad it knew, so a gap is filled on the next successful run.
4. Write new ads **straight into the database** with all card fields (kW,
   gearbox, body, town, dealer). This retires the CSV and the dashboard's
   import step.
5. Price each new ad immediately against the model, flag it, and put strong
   candidates on a **"new deals"** list (alerts come in Phase 7).
6. Health: record every scan; if scans fail for more than 15 minutes, show it
   on the dashboard (and send an alert later).

**Done when:** for a full day, every ad on the site's newest-first list was
captured. Check it with a one-off walk back through that day.

### Phase 2 — Complete, fresh Autoplius data (S, mostly waiting)
**Status: code done; first full pass running.** Findings on the way:
- The model list came from an old sitemap whose name-based URLs no longer
  work for hyphenated names (`mercedes-benz`, `land-rover`, `ct-200h`): the
  site returned all 46,676 cars, the title check rejected them, and Mercedes
  ended up with 66 of 3,749 cars. Searches now go by `make_id` / `model_id`
  from the site's full catalogue (~3,200 models; it was 1,057 of 26 makes).
- Starting coverage, measured with `scrape.py --coverage`: 81.1%.
- A faster pass (3 parallel requests, 1 s apart) got HTTP 429 and a CAPTCHA
  after ~350 models. Now: 1 request every ~2 s, rate limits waited out, a
  nightly limit of 500 models so the weekly refresh is spread out.
- Damage sweep: 2,343 damaged cars on the site, read in ~120 pages.
1. Switch the damage sweep to the **site-wide** damaged search: about 1,500
   cars ≈ 75 pages, instead of 1,057 searches.
2. Run a full re-scrape to fill kW / gearbox / body. Rebuild the report and
   compare model accuracy before and after.
3. Look into the 37k vs 46.5k gap: compare the site's per-model counts with
   the database, and check what the title-match filter rejects.
4. Schedule a nightly refresh of models older than 7 days, so price changes
   and sold cars ("gone") are tracked.

**Done when:** the database holds ≥ 95% of the site's count, and kW is known
for most cars.

### Phase 3 — Multi-site database (M)
**Status: done 2026-11-03.** Listings, price history and models carry a
`source`; listings are keyed by (source, id). `model_aliases` maps a site's
make/model names to the canonical ones. The code is in `scripts/collectors/`
(common, store, crawl, watch + one module per site), driven by `scrape.py`
and `watch.py`. Older databases upgrade themselves on first open (verified
on the real file: every count identical, backup kept).
1. New schema: `listings(source, source_id, …)`, `price_history(source,
   source_id, …)`, `makes_models` catalogue with per-site aliases.
2. One-off migration of the current Autoplius data (`source = 'autoplius'`).
3. Move the Autoplius code into `collectors/autoplius.py` on top of shared
   `collectors/common.py` (fetch, block detection, store, watcher loop).

**Done when:** everything from Phases 1–2 works the same on the new schema.

### Phase 4 — autogidas.lt collector (M–L)
**Status: done 2026-11-04** (first full pass of the site pending).
`scripts/collectors/autogidas.py`. Differences from the plan: models aren't
available without the robots-disallowed `/ajax` / `/select` endpoints, so
the site is searched per make and each car's model is read from its card
title and matched against the shared catalogue (60 of 60 sample titles
matched a known model). Ad pages, damage sweep (7 defect types, minor
defects left out) and the watcher work as planned; tested live on a copy.

Phase 3 re-check, done at the same time: the ad-page reader crashed on a
missing import (never exercised by a test or a live run until now; fixed,
and both sites' ad readers are now tested), and "gone" detection could not
work for a site searched per make (fixed, tested). A static check
(pyflakes) is clean.
1. Card parser and ad-page parser (description, parameter table, VIN where
   shown), tested against fixtures.
2. Full scrape: walk by make/model (or price bands), the same resumable way.
3. Damage sweep: the site-wide `f_46` search, once per defect type
   (Daužtas, Degęs, Skendęs, engine, gearbox, hail, other major, other minor).
4. Watcher with `f_50=naujausi_desc`, using the same gap-free logic.
5. Map autogidas make/model names into the shared catalogue.
6. Read autogidas' terms of use before running it regularly. Keep request
   rates as polite as for Autoplius.

**Done when:** both sites appear in the report and the price model trains on
both.

### Phase 5 — Combine the sites properly (M)
1. **Cross-site duplicates:** many dealers post the same car on both sites.
   Match on VIN where known; otherwise make, model, year, km (±1%), kW, town
   and price.
2. Count a duplicated car once in the price model, but show both listings.
   "Listed on both, €400 cheaper on autogidas" is itself a useful signal.
3. Add `source` as a model feature, in case one site's asking prices run
   systematically higher.

**Done when:** a car posted on both sites appears as one row with two links.

### Phase 6 — Bargain vs crashed: sharper classification (M)
Today a car is either "flagged" or not. The goal is a clear verdict on every
candidate:

| Verdict | Meaning |
|---|---|
| **Deal** | Under its p10, nothing found after reading the ad |
| **Damaged** | Defect declared on the site (and which kind) |
| **Problem** | Problem words found: not running, no TA, export, parts, lease transfer |
| **Not a real price** | Lease payment, deposit, typo, parts car (< 30% of expected) |
| **Unchecked** | Looks cheap, ad not read yet |

1. Read every new ad automatically when it lands under its p10, so
   "Unchecked" stays near empty.
2. Optional: classify the Lithuanian / Russian description with an AI model
   (the Claude API) for damage severity ("minor scratch" vs "after accident")
   on top of the keyword list. It costs fractions of a cent per ad, and is
   only run on candidates.
3. **Admin feedback:** buttons to mark a car "crashed", "real deal" or "fake
   price". Labels are stored and used to tune the flags and to measure them.
4. Feedback from the market: do "Deal" cars disappear faster? The sold-speed
   view measures this once scrapes are a few days apart.

**Done when:** every car under its p10 has a verdict other than Unchecked,
and you agree with the verdict on a spot check of 50.

### Phase 7 — Web server with admin and user roles (L)
1. FastAPI app that replaces `scraper_dashboard.py`; the scheduler from §3
   runs inside it.
2. **Viewer pages (`/`):** deals list (paginated, filters, verdicts), car
   detail, groups, price-by-year, price drops. Data comes from read-only JSON
   endpoints, not an 8 MB page.
3. **Admin pages (`/admin`):** jobs (start / stop / schedule), live logs, scan
   health, model list, user list, audit log ("who started what, when"), the
   verdict-labelling buttons.
4. Login: check Cloudflare Access' signed token on every request; roles come
   from a config file (admin / viewer). Unknown emails are refused, and the
   server refuses admin endpoints to viewers.
5. Hardening: a fixed job list (the browser can never send commands), input
   validation, strict Content-Security-Policy and security headers, CSRF
   checks, a read-only database connection for viewers.
6. Alerts: Telegram or email for "new deal", "watcher failing" and "site
   refusing requests".

**Done when:** it runs locally with test logins for both roles, and a viewer
can't trigger any admin action even by calling the API directly.

### Phase 8 — Go live from the PC (S–M)
1. Domain on Cloudflare (~€10/year). Run `cloudflared` as a Windows service.
2. Two Cloudflare Access policies: `/admin*` for your email only, with 2FA and
   a short session; `/` for the viewer list.
3. Run the app and tunnel under a separate standard Windows user. Start on
   boot, Windows updates on, BitLocker on.
4. Daily database backup (copy to a second disk or cloud folder), and a
   health check that alerts you if the site or a watcher goes down.
5. Write down how to add or remove users and how to restore a backup.

**Done when:** you can start a job from your phone on mobile data, and a
viewer can sign in and browse deals.

---

## 5. Decisions you need to make

1. **Sharing scraped data:** check both sites' terms before giving others
   access, and be stricter still if access would ever be paid. A small private
   group is the low-risk version.
2. **Domain name** for the site (Phase 8).
3. **What viewers see:** seller descriptions? links to the original ads?
   admin labels?
4. **Alerts:** Telegram, email, or both.
5. **AI description check** (Phase 6.2): worth a small API cost?

## 6. Suggested order

Phase **0 → 1 → 2** first. They fix the data you already have and the missed
new ads, and need no new infrastructure. Then **3 → 4 → 5**: get both sites
into one clean database *before* building the website on top of it, so the
web app is built once. Then **6**, and finally **7 → 8** to put it online.
