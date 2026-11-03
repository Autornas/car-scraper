#!/usr/bin/env python3
"""Run the listing watcher with a local live status dashboard."""

import argparse
import json
import json
import sqlite3
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests

import watch_new_listings as watcher
import autoplius_scraper as scraper

from settings import SETTINGS, project_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = project_path(SETTINGS["paths"]["data_dir"])
DEFAULT_STATE = DATA_DIR / "new_listings_seen.sqlite"

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Autoplius watcher</title>
<style>
:root{color-scheme:dark;--bg:#10151d;--panel:#18212c;--line:#2a3746;--text:#edf3f8;--muted:#9aabba;--accent:#62d6b5;--warn:#f6c76b;--bad:#ff8c8c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:32px 20px 60px}h1{margin:0;font-size:30px}h2{font-size:18px;margin:0 0 14px}
.sub,.muted{color:var(--muted)}.sub{margin:5px 0 24px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:18px}
.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}.label{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.value{font-size:21px;font-weight:650;margin-top:4px;overflow-wrap:anywhere}.panel{margin-top:14px}.phase{color:var(--accent)}.error{color:var(--bad)}.ok{color:var(--accent)}.baseline{color:var(--warn)}
.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:700px}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}a{color:var(--accent)}details summary{cursor:pointer;color:var(--accent)}ul{margin:8px 0;padding-left:20px}
.pill{display:inline-block;padding:2px 8px;border:1px solid var(--line);border-radius:999px;font-size:12px}.small{font-size:13px}.right{float:right;color:var(--muted)}
@media(max-width:600px){main{padding:20px 12px}.right{float:none;display:block}}
</style></head>
<body><main>
<h1>Autoplius listing watcher</h1>
<p class="sub">Local dashboard · scans the latest result pages every five minutes · <span id="updated">Connecting…</span></p>
<section class="cards">
 <div class="card"><div class="label">Watcher</div><div class="value" id="watcher">Starting</div></div>
 <div class="card"><div class="label">Current scan</div><div class="value" id="current">Waiting</div></div>
 <div class="card"><div class="label">Last completed scan</div><div class="value" id="lastScan">—</div></div>
 <div class="card"><div class="label">Latest scan results</div><div class="value" id="scanCounts">—</div></div>
 <div class="card"><div class="label">Next scan</div><div class="value" id="nextScan">—</div></div>
</section>
<section class="panel"><h2>What this scan checked</h2><div id="progress" class="muted">Waiting for scan…</div></section>
<section class="panel"><h2>Newly discovered listings</h2><div id="newListings" class="muted">None recorded yet.</div></section>
<section class="panel"><h2>Recent scan history</h2><div class="table-wrap"><table><thead><tr><th>Started</th><th>Result</th><th>Pages checked</th><th>Listings seen</th><th>New IDs</th><th>Details</th></tr></thead><tbody id="history"></tbody></table></div></section>
<p class="muted small">“New” means a listing ID appeared in the monitored result pages since an earlier successful scan. The site upload time is not verified. Keep this terminal running; Ctrl+C stops the watcher and dashboard.</p>
</main>
<script>
const $=id=>document.getElementById(id);
function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function local(s){if(!s)return "—";let d=new Date(typeof s==="number"?s*1000:s);return isNaN(d)?esc(s):d.toLocaleString()}
function statusClass(s){return s==="ok"?"ok":s==="baseline"?"baseline":s==="failed"?"error":"phase"}
function render(data){
 const c=data.current||{}; const runs=data.runs||[]; const last=runs[0];
 $("updated").textContent="Updated "+new Date().toLocaleTimeString();
 $("watcher").innerHTML='<span class="'+(data.running?"ok":"error")+'">'+(data.running?"Running":"Stopped")+'</span>';
 $("current").textContent=c.phase==="fetching"?`Fetching page ${c.page}/${c.pages}`:c.phase==="page_complete"?`Page ${c.page}/${c.pages} done`:(c.phase||"Waiting");
 $("lastScan").textContent=last?local(last.finished_at):"No completed scans yet";
 $("scanCounts").textContent=last?`${last.listings_seen} seen · ${last.new_count} new`:"—";
 $("nextScan").textContent=data.next_scan?local(data.next_scan):"—";
 $("progress").innerHTML=c.phase==="fetching"?`Requesting <a href="${esc(c.url)}" target="_blank" rel="noreferrer">${esc(c.url)}</a> · checked ${c.page-1}/${c.pages} pages so far`:c.phase==="page_complete"?`Page ${c.page}/${c.pages}: ${c.page_listing_count} listings · ${c.listings_seen} distinct listings seen so far`:c.phase==="complete"?`<span class="${statusClass(c.status)}">${esc(c.message)}</span> · ${c.pages_checked} page(s), ${c.listings_seen} listings`:esc(c.message||"Waiting for the first scan…");
 const discovered=last?.details?.new_listings||[];
 $("newListings").innerHTML=discovered.length?'<ul>'+discovered.map(x=>`<li><a href="${esc(x.link)}" target="_blank" rel="noreferrer">${esc(x.title||"Listing "+x.id)}</a> · ${esc(x.year)} · ${x.price_eur?esc(x.price_eur)+" €":"price unavailable"} <span class="muted">(#${esc(x.id)})</span></li>`).join("")+'</ul>':"No new IDs in the latest completed scan.";
 $("history").innerHTML=runs.length?runs.map(r=>`<tr><td>${local(r.started_at)}</td><td><span class="${statusClass(r.status)}">${esc(r.message)}</span></td><td>${r.pages_checked}</td><td>${r.listings_seen}</td><td>${r.new_count}</td><td><details><summary>Pages</summary><ul>${(r.details.pages||[]).map(p=>`<li>Page ${p.page}: ${p.status==="ok"?`${p.listing_count} listings`:"request failed"}${p.status==="ok"&&p.listings?.length?` — ${p.listings.map(x=>`<a href="${esc(x.link)}" target="_blank" rel="noreferrer">#${esc(x.id)}</a>`).join(", ")}`:""}</li>`).join("")}</ul></details></td></tr>`).join(""):'<tr><td colspan="6" class="muted">No scan history yet.</td></tr>';
}
async function refresh(){try{const r=await fetch("/api/status",{cache:"no-store"});if(!r.ok)throw Error(r.status);render(await r.json())}catch(e){$("watcher").innerHTML='<span class="error">Dashboard connection lost</span>';$("updated").textContent=String(e)}}
refresh();setInterval(refresh,2000);
</script></body></html>"""


class DashboardState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = True
        self.current = {"phase": "starting"}
        self.next_scan = None

    def update(self, **values):
        with self.lock:
            self.current = {**self.current, **values}

    def set_next_scan(self, timestamp):
        with self.lock:
            self.next_scan = timestamp

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "current": dict(self.current),
                "next_scan": self.next_scan,
            }


def scan_loop(dashboard, stop_event, args):
    state = watcher.SeenStore(args.state)
    output_ids = scraper.load_existing_ids(args.output)
    sink = scraper.CsvSink(args.output, scraper.FIELDS)
    interval = args.interval_minutes * 60

    try:
        with requests.Session() as session:
            while not stop_event.is_set():
                dashboard.update(phase="starting", message="Scan starting…")
                try:
                    watcher.scan_once(
                        session, state, sink, output_ids, args.url, args.pages,
                        args.delay,
                        on_progress=lambda values: dashboard.update(**values),
                    )
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    dashboard.update(phase="error", message=message)
                    print(f"Watcher scan failed: {message}", flush=True)
                dashboard.set_next_scan(time.time() + interval)
                stop_event.wait(interval)
    finally:
        with dashboard.lock:
            dashboard.running = False
        sink.close()
        state.close()


def read_history(path, limit=25):
    path = Path(path)
    if not path.exists():
        return []
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        rows = connection.execute(
            """SELECT id, started_at, finished_at, status, pages_checked,
                      listings_seen, new_count, details_json, message
               FROM scan_history ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if connection is not None:
            connection.close()
    names = (
        "id", "started_at", "finished_at", "status", "pages_checked",
        "listings_seen", "new_count", "details_json", "message",
    )
    result = []
    for row in rows:
        item = dict(zip(names, row))
        item["details"] = json.loads(item.pop("details_json"))
        result.append(item)
    return result


def make_handler(dashboard, state_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = urlparse(self.path).path
            if route == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif route == "/api/status":
                payload = dashboard.snapshot()
                payload["runs"] = read_history(state_path)
                body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            else:
                self.send_error(404)
                return
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Run a local live watcher dashboard.")
    parser.add_argument("--url", default=scraper.DEFAULT_SEARCH)
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--interval-minutes", type=float, default=5)
    parser.add_argument("--delay", type=float, default=1)
    parser.add_argument("--output", default=str(DATA_DIR / "new_listings.csv"))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the dashboard automatically")
    args = parser.parse_args()
    if args.pages < 1:
        parser.error("--pages must be at least 1")
    if args.interval_minutes <= 0:
        parser.error("--interval-minutes must be greater than 0")
    if args.delay < 0:
        parser.error("--delay cannot be negative")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    dashboard = DashboardState()
    stop_event = threading.Event()
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port), make_handler(dashboard, args.state)
    )
    scanner = threading.Thread(
        target=scan_loop, args=(dashboard, stop_event, args),
        name="listing-watcher", daemon=True,
    )
    scanner.start()
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Live watcher dashboard: {url}", flush=True)
    print("Bound to localhost only. Keep this terminal open; Ctrl+C stops it.",
          flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping dashboard and watcher…", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        stop_event.set()
        scanner.join(timeout=10)


if __name__ == "__main__":
    main()
