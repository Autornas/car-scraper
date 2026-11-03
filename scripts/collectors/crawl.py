"""
collectors/crawl.py — the full scrape, written once for every site.

A site module (collectors.autoplius, later collectors.autogidas) supplies
URLs and parsers; this walks each model's search page by page, splits searches
that hit the pagination cap by price, sweeps the site's damaged listings,
reads ad pages for descriptions, and measures coverage.

`site` is the site module, `db` its SiteStore (store.site(site.SOURCE)).
"""

import sys
import time
from concurrent.futures import ThreadPoolExecutor

from collectors.common import STOP, diagnose, fetch, probe, title_fits
from collectors.store import find_gone


def scrape_search(site, url, make, model, db, session, blocks, args, label=""):
    """
    Walk the pages of one search URL, upserting each page.

    Page 1 is read alone first: its result count says how many pages exist,
    so a 5-car model costs one request, not a batch. An empty search ends
    there. Further pages are fetched `--concurrency` at a time, and the walk
    still stops when a batch brings no new ids (the site re-serves page 1 for
    page numbers past the end).

    Returns (ids_seen, complete). complete is False if a block or Ctrl-C cut
    the walk short, so the caller knows not to mark the model as done.
    """
    ids = set()
    known_models = db.models_for_make(make)
    rejected = 0

    def take(html):
        nonlocal rejected
        page_rows = list(site.scrape_page(html))
        rows = []
        for row in page_rows:
            if not title_fits(row.get("title", ""), make, model, known_models):
                rejected += 1
                continue
            row["_make"], row["_model"] = make, model
            rows.append(row)
        fresh = [r for r in rows if r["id"] not in ids]
        ids.update(r["id"] for r in rows)
        db.upsert_many(fresh, make, model)
        return len(page_rows), len(fresh)

    if STOP.is_set() or blocks.tripped.is_set():
        return ids, False
    first = fetch(session, site.page_url(url, 1), blocks=blocks)
    if not first:
        print(f"    {label}page 1: no response — stopping")
        return ids, False
    total = site.result_count(first)
    if total == 0:
        print(f"    {label}no listings")
        return ids, True
    on_page, _ = take(first)
    if not on_page:
        if total is None:
            # Not a results page at all (a challenge served with status 200):
            # treat it as a refusal, never as "this model has no cars".
            print(f"    {label}page 1 is not a results page — stopping", file=sys.stderr)
            blocks.record_refusal(200, first)
            return ids, False
        print(f"    {label}page 1 held no listing cards")
        return ids, True
    last_page = args.max_pages
    if total:
        last_page = min(args.max_pages, -(-total // max(on_page, 1)))

    complete = True
    page = 2
    conc = max(1, args.concurrency)
    while page <= last_page:
        if STOP.is_set() or blocks.tripped.is_set():
            complete = False
            break
        time.sleep(args.delay)
        batch = list(range(page, min(page + conc, last_page + 1)))
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            htmls = list(pool.map(
                lambda n: fetch(session, site.page_url(url, n), blocks=blocks), batch))
        if not any(htmls):
            print(f"    {label}pages {batch[0]}-{batch[-1]}: no response — stopping")
            complete = False
            break
        gained = sum(take(h)[1] for h in htmls if h)
        if gained == 0:
            break
        page += conc

    note = f", ignored {rejected} not matching {make}/{model}" if rejected else ""
    print(f"    {label}{len(ids)} listing(s)"
          + (f" of {total}" if total else "") + note)
    return ids, complete


def scrape_model(site, url, make, model, db, session, blocks, args):
    """Scrape one model; if it hits the pagination cap, split it by price."""
    ids, complete = scrape_search(site, url, make, model, db, session, blocks, args)
    capped = len(ids) >= args.cap_threshold

    if capped:
        print(f"    {make}/{model} hit the cap ({len(ids)}) — splitting by price")
        queue = [(0, 9999999)]
        while queue:
            if STOP.is_set() or blocks.tripped.is_set():
                break
            lo, hi = queue.pop(0)
            sub, sub_ok = scrape_search(site, site.band_url(url, lo, hi), make, model, db,
                                        session, blocks, args, label=f"EUR {lo}-{hi} ")
            ids |= sub
            complete = complete and sub_ok
            if len(sub) >= args.cap_threshold and (hi - lo) > args.min_width:
                mid = lo + (hi - lo) // 2
                queue.insert(0, (mid + 1, hi))
                queue.insert(0, (lo, mid))

    if complete and not STOP.is_set() and not blocks.tripped.is_set():
        db.mark_model(url, make, model, len(ids), capped)
    else:
        print(f"    {make}/{model} was cut short — left unmarked so the next "
              f"run retries it")
    return ids


def damage_sweep(site, db, session, blocks, args):
    """
    Every car on the site whose seller declared a defect, read from the site's
    own defect filter (Autoplius: one search with every defect type ticked,
    ~2,300 cars on ~120 pages). Replaces one damaged search per model.
    Returns the number of this site's damaged listings in the database, or
    None if the sweep was cut short (then nothing is changed).
    """
    ids = set()
    print("Damage sweep: reading every listing with a declared defect")
    for search in site.damaged_search_urls():
        total, before_search = None, len(ids)
        for page in range(1, args.max_pages + 1):
            if STOP.is_set() or blocks.tripped.is_set():
                return None
            html = fetch(session, site.page_url(search, page), blocks=blocks)
            if not html:
                return None
            if page == 1:
                total = site.result_count(html)
                if total is not None and total > args.max_pages * site.PAGE_SIZE:
                    print(f"  ! {total} damaged listings is more than {args.max_pages} "
                          "pages can show; the sweep will be partial", file=sys.stderr)
            before = len(ids)
            rows = list(site.scrape_page(html))
            if not rows and site.result_count(html) is None:
                # A challenge page, not results: stop without touching any flag,
                # or every car would be marked undamaged.
                blocks.record_refusal(200, html)
                return None
            ids.update(row["id"] for row in rows)
            found = len(ids) - before_search
            if len(ids) == before or (total is not None and found >= total):
                break
            if page % 20 == 0:
                print(f"  page {page}: {found} of {total or '?'}")
            time.sleep(args.delay)
    flagged = db.apply_damage_sweep(ids)
    print(f"Damage sweep: {len(ids)} damaged on the site, {flagged} of them in the database")
    return flagged


def fetch_missing_descriptions(site, db, session, blocks, delay, limit=None, ids=None):
    """Fetch descriptions (and ad-page details) for unchecked listings and save
    progress per ad. With `ids`, only those — e.g. data/candidates.txt."""
    todo = db.missing_descriptions(limit, ids)
    print(f"{len(todo)} listing description(s) queued.")
    checked = 0
    for index, (ad_id, link) in enumerate(todo, start=1):
        if STOP.is_set() or blocks.tripped.is_set():
            break
        if not link:
            print(f"[{index}/{len(todo)}] {ad_id}: no listing URL; skipping")
            db.save_description(ad_id, None)
            continue
        try:
            description, details, completed, status = site.fetch_ad_details(
                session, link, blocks
            )
        except ValueError as exc:
            print(f"[{index}/{len(todo)}] {ad_id}: {exc}", file=sys.stderr)
            db.save_description(ad_id, None)
            continue
        if not completed:
            if status == 200:
                print("Stopped on a browser verification challenge.")
            else:
                print("Stopped after a request failure; remaining ads are "
                      "left queued for a later run.")
            break
        db.save_description(ad_id, description, details)
        checked += 1
        result = "description saved" if description else "no description"
        if details.get("damage_type"):
            result += f" · defect: {details['damage_type']}"
        print(f"[{index}/{len(todo)}] {ad_id}: {result}")
        if index < len(todo) and delay:
            time.sleep(delay)
    print(f"Descriptions checked this run: {checked}")
    print(f"Descriptions still queued: {db.descriptions_remaining()}")
    return checked


def do_coverage(site, store, session):
    """How much of what the site lists right now is in the database, per make.
    'In the database' means stored and not gone (see find_gone)."""
    counts = site.fetch_make_counts(session)
    if not counts:
        print("Could not read the site's make counts.")
        return None
    gone = {ad_id for _, ad_id in find_gone(store, site.SOURCE)}
    have = {}
    with store.lock:
        for ad_id, make in store.conn.execute(
                "SELECT id, make FROM listings WHERE source = ?", (site.SOURCE,)):
            if ad_id not in gone:
                have[make] = have.get(make, 0) + 1
    site_total = sum(counts.values())
    db_total = sum(min(have.get(mk, 0), n) for mk, n in counts.items())
    print(f"{site.SOURCE} lists {site_total:,} used cars; the database holds "
          f"{db_total:,} of them ({db_total / max(site_total, 1):.1%}).\n")
    gaps = sorted(((n - have.get(mk, 0), mk, n) for mk, n in counts.items()), reverse=True)
    print("Biggest gaps (site / database):")
    for missing, mk, n in gaps[:15]:
        if missing <= 0:
            break
        print(f"  {mk:<20} {n:6,} / {have.get(mk, 0):6,}   missing {missing:,}")
    return db_total / max(site_total, 1)


def do_diagnose(site, session):
    """Check which parts of the site answer THIS machine, and say what's wrong."""
    targets = site.diagnose_targets()
    print("Checking what answers this machine:\n")
    ok = []
    for label, url in targets:
        status, body = probe(session, url)
        if status == 200:
            ok.append(label)
            print(f"  OK    {label}")
        else:
            shown = status if status else body
            print(f"  FAIL  {label}  ->  {shown}")
            if status and isinstance(body, str) and body.strip():
                print(f"          looks like: {diagnose(body)}")
    print()
    if not ok:
        print("Nothing answered at all. That is an IP-level refusal, not a bug in")
        print("this script. If you are on a VPN, turn it off and try again — VPN and")
        print("datacenter IPs get challenged on sight regardless of how slowly you ask.")
    elif len(ok) < len(targets):
        print("Some paths answer and others don't, so your IP is fine — one specific")
        print("path is blocked; wait a while before scraping that part again.")
    else:
        print("Everything answered. Re-run the scrape.")
