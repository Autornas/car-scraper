"""
web/admin.py — the admin area: the control page, the task runner, the
database summary, the model editor, labels and the audit log.

web/app.py serves all of this to admins only.
"""

import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from collectors import SITES, alerts, enabled_sites, watchlist
from collectors.verdicts import LABELS, set_label
from collectors.store import Store
from collectors.watch import ensure_tables as ensure_watch_tables
from collectors.watch import health as watcher_health

from settings import SETTINGS, project_path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT_ROOT / "scripts"
DATA_DIR = project_path(SETTINGS["paths"]["data_dir"])
DB_PATH = project_path(SETTINGS["paths"]["database"])
REPORT_PATH = project_path(SETTINGS["paths"]["reports_dir"]) / "report.html"
PUBLIC_REPORT_PATH = project_path(SETTINGS["paths"]["reports_dir"]) / "report_public.html"
CANDIDATES_PATH = DATA_DIR / "candidates.txt"


def _script(name, *args):
    return [sys.executable, "-u", str(SCRIPTS / name), *args]


# action -> (task name shown in the dashboard, [command, command, ...]).
# Commands run one after another; a failed or stopped step ends the task.
# update.py is the whole routine in one task: both sites scraped at the same
# time, damage sweep, the best candidates' ads read, report rebuilt. The
# separate damage / candidate tasks it replaced were steps of it anyway.
TASKS = {
    "start_watcher": ("watcher", [
        _script("watch.py")]),
    "update": ("update", [_script("update.py")]),
    "rescrape_all": ("full re-scrape", [_script("update.py", "--force")]),
    "build_report": ("report build", [_script("autoplius_stats.py")]),
}

# Scrapers start every line with "[site] " (collectors.common.site_output).
SITE_LINE_RE = re.compile(r"\s*\[(\w+)\] (.*)", re.S)
# A site saying no, or a scan that got nothing ...
PROBLEM_RE = re.compile(
    r"HTTP (401|403|429|5\d\d)|refus|challenge|captcha|no response|not a results page"
    r"|could be read|cut short|newest-first|ConnectionError|Timeout|slow down"
    r"|request failure|is not an ad page", re.I)
# ... and a line that proves the site is answering again.
WORKING_RE = re.compile(
    r"\d+ new ad\(s\)|\d+ listing\(s\)|no listings|description saved|no description"
    r"|Damage sweep: \d+|Catalogue: \d+", re.I)
# Lines after a problem before a site counts as fine again without such proof.
CLEAR_AFTER = 4
REFUSED_HOSTS = {"autoplius.lt", "autogidas.lt"}


def _spawn(command):
    return subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def _describe(command):
    return " ".join([Path(command[2]).name, *command[3:]])

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Car scraper</title>
<style>
:root{color-scheme:light;--bg:#f6f6f4;--panel:#fff;--panel-2:#f0f0ed;--line:#dcdcd8;--text:#111;
 --muted:#6d6c68;--accent:#2a78d6;--ok:#0b8a0b;--bad:#c93c3c;--warn:#b86a12;--log:#fbfbfa}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#151514;--panel:#1d1d1c;--panel-2:#262624;
 --line:#363634;--text:#f2f2ef;--muted:#9c9b94;--accent:#4a95ec;--ok:#46c446;--bad:#ff7b7b;--warn:#f0b35a;--log:#121211}}
*{box-sizing:border-box}[hidden]{display:none!important}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,"Segoe UI",sans-serif;
 display:flex;flex-direction:column}
button{font:inherit;font-size:13px;font-weight:600;border-radius:7px;padding:7px 12px;cursor:pointer;
 border:1px solid var(--line);background:var(--panel-2);color:var(--text);white-space:nowrap}
button:hover:not(:disabled){border-color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.danger{background:var(--bad);border-color:var(--bad);color:#fff}
button:disabled{opacity:.45;cursor:not-allowed}
a{color:var(--accent)}code{font-size:12px}
/* top bar: always visible, carries the one thing you need from anywhere — what is running */
.top{display:flex;align-items:center;gap:14px;padding:8px 16px;background:var(--panel);
 border-bottom:1px solid var(--line);flex-wrap:wrap}
.brand{font-weight:700;font-size:15px}
.nav{display:flex;gap:2px}
.nav button{background:none;border:0;border-radius:6px;font-weight:500;color:var(--muted);padding:7px 12px}
.nav button[aria-current="page"]{background:var(--panel-2);color:var(--text);font-weight:600}
.spacer{flex:1}
.chip{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--muted)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--line)}
.chip.running .dot{background:var(--ok);box-shadow:0 0 0 3px color-mix(in srgb,var(--ok) 25%,transparent)}
.chip.running{color:var(--text)}
.chip.failed .dot{background:var(--bad)}
main{flex:1;min-height:0;display:flex}
.view{flex:1;min-width:0;min-height:0;padding:14px 16px}
/* control view: tasks + numbers on the left, live output filling the right */
.control{display:grid;grid-template-columns:minmax(360px,460px) minmax(0,1fr);gap:14px}
.col{min-height:0;display:flex;flex-direction:column;gap:12px;overflow:auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0;
 padding:10px 14px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:10px}
.stats{display:grid;grid-template-columns:repeat(3,1fr)}
.stat{padding:9px 14px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}
.stat:nth-child(3n){border-right:0}.stat:nth-last-child(-n+3){border-bottom:0}
.stat .v{font-size:17px;font-weight:650;font-variant-numeric:tabular-nums;white-space:nowrap}
.stat .l{font-size:12px;color:var(--muted)}
.group-label{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
 padding:10px 14px 2px}
.task{display:flex;align-items:center;gap:12px;padding:9px 14px}
.task+.task{border-top:1px solid var(--line)}
.task .t{flex:1;min-width:0}
.task .n{font-weight:600}
.task .d{font-size:12px;color:var(--muted)}
.task.running{background:color-mix(in srgb,var(--ok) 9%,transparent)}
.task.running .n::after{content:" · running";color:var(--ok);font-weight:500}
details.more{border-top:1px solid var(--line)}
details.more summary{cursor:pointer;padding:9px 14px;font-size:13px;color:var(--muted);user-select:none}
details.more[open] summary{border-bottom:1px solid var(--line)}
.right{min-height:0;display:flex;flex-direction:column;gap:12px}
.log{flex:1;min-height:0;display:flex;flex-direction:column}
/* one output pane per site, side by side: who printed it is never in doubt */
.panes{flex:1;min-height:0;display:flex}
.pane{flex:1;min-width:0;min-height:0;display:flex;flex-direction:column}
.pane+.pane{border-left:1px solid var(--line)}
.pane .ph{display:flex;align-items:baseline;gap:8px;padding:7px 14px;border-bottom:1px solid var(--line);
 font-size:13px;flex-wrap:wrap}
.pane .ph b{font-weight:650}
.pane .st{font-size:12px;color:var(--muted);overflow-wrap:anywhere}
.pane .st.ok{color:var(--ok)}.pane .st.bad{color:var(--bad);font-weight:600}
.pane pre{border-radius:0}.pane:first-child pre{border-bottom-left-radius:10px}
.pane:last-child pre{border-bottom-right-radius:10px}
.logtabs{display:flex;gap:2px;text-transform:none;letter-spacing:0}
.logtabs button{padding:2px 9px;font-size:12px;font-weight:500}
.logtabs button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.auditlist{font-size:12px;padding:6px 14px 10px}
.auditlist div{padding:3px 0;border-top:1px solid var(--line)}
.auditlist div:first-child{border-top:0}
.deals{flex:0 1 auto;max-height:45%;min-height:0;display:flex;flex-direction:column}
.deals .list{overflow:auto;min-height:0}
.deal{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:4px 12px;align-items:baseline;
 padding:8px 14px;border-top:1px solid var(--line);font-size:13px}
.deal:first-child{border-top:0}
.deal .t{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.deal .m{grid-column:1/-1;font-size:12px;color:var(--muted)}
.deal .p{font-variant-numeric:tabular-nums;font-weight:650}
.v{font-size:11px;font-weight:650;padding:1px 7px;border-radius:99px;white-space:nowrap}
.v.deal{background:color-mix(in srgb,var(--ok) 18%,transparent);color:var(--ok)}
.v.damaged,.v.problem{background:color-mix(in srgb,var(--bad) 16%,transparent);color:var(--bad)}
.v.other{background:var(--panel-2);color:var(--muted)}
.v.maybe{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn)}
.empty{padding:14px;color:var(--muted);font-size:13px}
.health.bad{color:var(--bad);font-weight:600}.health.ok{color:var(--ok)}
.log h2 .spacer{flex:1}
.log label{text-transform:none;letter-spacing:0;font-size:12px;display:flex;align-items:center;gap:5px}
pre{flex:1;min-height:0;margin:0;padding:10px 14px;overflow:auto;background:var(--log);
 border-radius:0 0 10px 10px;font:12.5px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
pre .e{color:var(--bad)}pre .s{color:var(--accent);font-weight:600}
/* report view: the report fills the area; no outer scrolling */
.report{padding:0;position:relative;display:flex}
.report iframe{flex:1;border:0;background:var(--bg)}
.overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
 flex-direction:column;gap:6px;background:var(--bg);color:var(--muted);text-align:center;padding:20px}
.overlay strong{color:var(--text);font-size:15px}
/* models view */
.models{display:flex;flex-direction:column;gap:10px;max-width:900px}
.models textarea{flex:1;min-height:200px;width:100%;resize:none;background:var(--panel);color:var(--text);
 border:1px solid var(--line);border-radius:10px;padding:12px;font:13px/1.5 ui-monospace,Consolas,monospace}
.models p{margin:0;color:var(--muted);font-size:13px}
.bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
/* watchlist view */
.watchlist{display:flex;flex-direction:column;gap:14px;max-width:900px;overflow:auto}
.watchlist p{margin:0;color:var(--muted);font-size:13px}
.field{display:flex;flex-direction:column;gap:4px;font-size:13px;font-weight:600}
.field small,.watchlist small{font-weight:400;color:var(--muted);font-size:12px}
.field input{font:inherit;font-weight:400;padding:8px 10px;border-radius:7px;border:1px solid var(--line);
 background:var(--panel);color:var(--text);min-width:0}
.field input:focus{outline:2px solid var(--accent);outline-offset:-1px}
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;align-items:end}
.check{display:flex;align-items:center;gap:8px;font-size:13px}
.backupline{padding:7px 14px;border-top:1px solid var(--line);font-size:12px;color:var(--muted);overflow-wrap:anywhere}
.backupline.bad{color:var(--bad);font-weight:600}
.deal .star{color:var(--warn)}
.ok{color:var(--ok)}.error{color:var(--bad)}
#alert{position:fixed;bottom:16px;right:16px;z-index:10;max-width:min(480px,calc(100vw - 32px));
 padding:12px 40px 12px 14px;background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--bad);
 border-radius:9px;box-shadow:0 10px 30px #0004}
#alert.success{border-left-color:var(--ok)}#alert.warning{border-left-color:var(--warn)}
#alertTitle{font-weight:650}#alertMessage{color:var(--muted);font-size:13px;overflow-wrap:anywhere}
#alertClose{position:absolute;top:6px;right:6px;background:none;border:0;font-size:18px;padding:2px 8px}
@media (max-width:900px){
 body{height:auto;min-height:100%}
 .deals{max-height:none}
 main{display:block}
 .control{display:flex;flex-direction:column}
 .col{overflow:visible}
 .log{height:auto;flex:none}
 .panes{flex-direction:column}.pane{height:45vh;flex:none}
 .pane+.pane{border-left:0;border-top:1px solid var(--line)}
 .report{height:calc(100vh - 100px)}
 .stats{grid-template-columns:repeat(2,1fr)}
 .stat:nth-child(3n){border-right:1px solid var(--line)}.stat:nth-child(2n){border-right:0}
 .stat:nth-last-child(-n+3){border-bottom:1px solid var(--line)}.stat:nth-last-child(-n+2){border-bottom:0}
}
</style></head>
<body>
<header class="top">
 <span class="brand">Car scraper</span>
 <nav class="nav" aria-label="Sections">
  <button data-view="control" aria-current="page">Control</button>
  <button data-view="report">Deals &amp; prices</button>
  <button data-view="watchlist">Watchlist</button>
  <button data-view="models">Models</button>
 </nav>
 <span class="spacer"></span>
 <span class="bar" id="reportTools" hidden><button id="reloadReport" title="Load the latest built report (Update database on the Control page builds a new one)">Reload report</button>
  <a href="/report" target="_blank" rel="noreferrer">Open in new tab</a></span>
 <span class="chip" id="chip"><span class="dot"></span><span id="chipText">Connecting…</span></span>
 <button class="danger" id="stopButton" hidden>Stop task</button>
 <a href="/" title="What viewers see">Viewer page</a>
 <span class="muted" id="whoami" style="font-size:12px"></span>
</header>

<main>
<section class="view control" id="v-control">
 <div class="col">
  <div class="card">
   <h2>Database</h2>
   <div class="stats">
    <div class="stat"><div class="v" id="sListings">—</div><div class="l">listings</div></div>
    <div class="stat"><div class="v" id="sModels">—</div><div class="l">models scraped</div></div>
    <div class="stat"><div class="v" id="sLast">—</div><div class="l">last model scraped</div></div>
    <div class="stat"><div class="v" id="sDamaged">—</div><div class="l" id="sDamagedL">damaged flagged</div></div>
    <div class="stat"><div class="v" id="sNew">—</div><div class="l">new ads, last 24 h</div></div>
    <div class="stat"><div class="v" id="sReport">—</div><div class="l">report built</div></div>
   </div>
   <div class="backupline" id="backupLine"></div>
  </div>
  <div class="card">
   <h2>Scrapers <span class="spacer"></span><span id="notice" style="text-transform:none;letter-spacing:0"></span></h2>
   <div class="task" data-task="watcher"><div class="t"><div class="n">New-listing watcher</div>
    <div class="d">Reads each site's newest ads every 3 minutes, prices them, and opens the cheap ones to check for defects.</div>
    <div class="d health" id="watchHealth"></div></div>
    <button data-action="start_watcher">Start</button></div>
   <div class="task" data-task="update"><div class="t"><div class="n">Update database</div>
    <div class="d">Everything in one go: scrapes what is due on both sites at the same time, flags damaged cars, reads the best candidates' ads and rebuilds the report. Stop any time; the next run resumes.</div></div>
    <button class="primary" data-action="update">Start</button></div>
   <details class="more"><summary>More</summary>
    <div class="task" data-task="full re-scrape"><div class="t"><div class="n">Full re-scrape</div>
     <div class="d">The same update, but every model again regardless of age. Several hours.</div></div>
     <button data-action="rescrape_all" data-confirm="Re-scrape every model? This takes several hours.">Start</button></div>
    <div class="task" data-task="report build"><div class="t"><div class="n">Rebuild report only</div>
     <div class="d">Refits the price model and refreshes the deal list without scraping. ~30 s.</div></div>
     <button data-action="build_report">Start</button></div>
    <div class="group-label">Recent actions</div>
    <div class="auditlist" id="audit"><div class="muted">Nothing yet.</div></div>
   </details>
  </div>
 </div>
 <div class="right">
 <div class="card deals">
  <h2>New deals <span class="logtabs"><button id="watchOnly" hidden title="Show only the cars that fit your watchlist">★ Watchlist only</button></span>
   <span class="spacer"></span><span id="dealsNote" style="text-transform:none;letter-spacing:0"></span></h2>
  <div class="list" id="deals"><div class="empty">Cars the watcher finds under their price range appear here.</div></div>
 </div>
 <div class="card log">
  <h2><span id="logTitle">Output</span>
   <span class="logtabs"><button id="logJob" class="on">Update</button><button id="logWatcher">Watcher</button></span>
   <span class="spacer"></span>
   <label><input type="checkbox" id="follow" checked> Follow output</label></h2>
  <div class="panes" id="panes"></div>
 </div>
 </div>
</section>

<section class="view report" id="v-report" hidden>
  <iframe id="reportFrame" title="Deals and price analysis"></iframe>
 <div class="overlay" id="reportOverlay"><strong>Loading report…</strong>
  <span>The report is about 2 MB; it is rebuilt by every update and every night.</span></div>
</section>

<section class="view watchlist" id="v-watchlist" hidden>
 <p>Say which new deals matter to you. Cars that fit get a ★ in New deals (with a <b>Watchlist only</b> filter there), and Telegram alerts are sent only for them. Leave everything empty to be told about every confirmed deal.</p>
 <label class="field">Makes and models
  <input id="wlMakes" type="text" spellcheck="false" placeholder="bmw, audi a4, volkswagen/passat">
  <small>Separated by commas. A make alone means all its models. Empty = every make.</small></label>
 <div class="fields">
  <label class="field">Price from, €<input id="wlPriceMin" type="number" min="0" step="100" placeholder="any"></label>
  <label class="field">Price to, €<input id="wlPriceMax" type="number" min="0" step="100" placeholder="any"></label>
  <label class="field">Year from<input id="wlYearMin" type="number" min="0" max="2100" placeholder="any"></label>
  <label class="field">Mileage up to, km<input id="wlKmMax" type="number" min="0" step="10000" placeholder="any"></label>
  <label class="field">At least % under the typical price<input id="wlDiscount" type="number" min="0" max="90" placeholder="any"></label>
 </div>
 <p><small>The typical price is the median of similar cars in the database (same model, year ±1, mileage ±30%), or the price model's expected price when there are fewer than 5 of them.</small></p>
 <label class="check"><input type="checkbox" id="wlMaybe"> Also alert for “maybe” cars (too few similar cars to confirm the deal)</label>
 <div class="bar"><button class="primary" id="saveWatchlist">Save watchlist</button>
  <button id="clearWatchlist">Clear</button>
  <span id="wlStatus" style="color:var(--muted)"></span></div>
 <p id="wlTelegram"></p>
</section>

<section class="view models" id="v-models" hidden>
 <p>Autoplius models the database scraper covers, one <code>make/model</code> per line (for example <code>audi/a3</code>).
 The list starts as every make and model on Autoplius. Remove a line to switch that model off (its saved listings stay); add it back to switch it on. Only names from the site's catalogue are accepted.
 autogidas.lt is scraped per make and switched on or off as a whole in <code>config.toml</code>.</p>
 <textarea id="modelsText" spellcheck="false" aria-label="Model list"></textarea>
 <div class="bar"><button class="primary" id="saveModels">Save model list</button>
  <span id="modelsStatus" style="color:var(--muted)">Loading…</span></div>
</section>
</main>

<aside id="alert" role="alert" hidden><button id="alertClose" aria-label="Dismiss">×</button>
 <div id="alertTitle"></div><div id="alertMessage"></div>
 <a id="alertLink" target="_blank" rel="noreferrer" hidden>Open the refused page</a></aside>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=n=>n==null?"—":Number(n).toLocaleString();
function ago(iso){
 if(!iso)return "—";
 const s=(Date.now()-new Date(iso).getTime())/1000;
 if(s<90)return "just now";if(s<5400)return Math.round(s/60)+" min ago";
 if(s<129600)return Math.round(s/3600)+" h ago";return Math.round(s/86400)+" days ago";
}

/* ---------- views ---------- */
let reportLoaded=false;
const VIEWS=["control","report","watchlist","models"];
function showView(view){
 if(!VIEWS.includes(view))view="control";
 document.querySelectorAll(".nav button").forEach(b=>b.toggleAttribute("aria-current",b.dataset.view===view));
 document.querySelectorAll(".nav button[aria-current]").forEach(b=>b.setAttribute("aria-current","page"));
 VIEWS.forEach(v=>$("v-"+v).hidden=v!==view);
 $("reportTools").hidden=view!=="report";
 if(view==="report"&&!reportLoaded)loadReport();
 history.replaceState(null,"","#"+view);
}
function loadReport(){
 reportLoaded=true;$("reportOverlay").hidden=false;
 $("reportFrame").src="/report?t="+Date.now();
}
$("reportFrame").addEventListener("load",()=>{if($("reportFrame").src)$("reportOverlay").hidden=true;});
$("reloadReport").addEventListener("click",loadReport);
document.querySelectorAll(".nav button").forEach(b=>b.addEventListener("click",()=>showView(b.dataset.view)));

/* ---------- notifications ---------- */
function notify(title,message,kind="error",link=""){
 $("alertTitle").textContent=title;$("alertMessage").textContent=message;
 $("alertLink").hidden=!link;if(link)$("alertLink").href=link;
 $("alert").className=kind;$("alert").hidden=false;
 if(kind==="success")setTimeout(()=>{$("alert").hidden=true;},6000);
}
$("alertClose").addEventListener("click",()=>$("alert").hidden=true);

/* ---------- actions ---------- */
async function act(action){
 try{
  const response=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action})});
  const result=await response.json();
  $("notice").textContent=result.ok?"":result.message||"";
  $("notice").className=result.ok?"":"error";
  if(!response.ok)notify("Could not do that",result.message||response.statusText);
  await refresh();
 }catch(e){notify("Dashboard request failed",String(e));}
}
document.querySelectorAll(".task button").forEach(b=>b.addEventListener("click",()=>{
 const watcher=b.dataset.action==="start_watcher";
 const action=b.dataset.running?(watcher?"stop_watcher":"stop"):b.dataset.action;
 if(!b.dataset.running&&b.dataset.confirm&&!confirm(b.dataset.confirm))return;
 act(action);
}));
$("stopButton").addEventListener("click",()=>act("stop"));
let logSource="job";
["logJob","logWatcher"].forEach(id=>$(id).addEventListener("click",()=>{
 logSource=id==="logJob"?"job":"watcher";lastLog="";
 $("logJob").classList.toggle("on",logSource==="job");
 $("logWatcher").classList.toggle("on",logSource==="watcher");
 refresh();
}));

/* ---------- models ---------- */
async function loadModels(){
 try{
  const response=await fetch("/api/models",{cache:"no-store"});
  const result=await response.json();
  if(!response.ok)throw Error(result.message||response.statusText);
  $("modelsText").value=result.text;
  $("modelsStatus").textContent=`${result.count} models`;
 }catch(e){$("modelsStatus").textContent="Could not load model list: "+e;$("modelsStatus").className="error";}
}
$("saveModels").addEventListener("click",async()=>{
 $("saveModels").disabled=true;
 try{
  const response=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"save_database_models",text:$("modelsText").value})});
  const result=await response.json();
  $("modelsStatus").textContent=result.message||"";
  $("modelsStatus").className=response.ok?"ok":"error";
  if(response.ok)$("modelsText").value=result.text;
 }catch(e){$("modelsStatus").textContent="Save failed: "+e;$("modelsStatus").className="error";}
 finally{$("saveModels").disabled=false;}
});

/* ---------- watchlist ---------- */
const WL_FIELDS=[["wlPriceMin","price_min"],["wlPriceMax","price_max"],["wlYearMin","year_min"],
 ["wlKmMax","km_max"],["wlDiscount","min_discount"]];
function fillWatchlist(j){
 const w=j.watchlist;
 $("wlMakes").value=w.makes.join(", ");
 for(const [id,key] of WL_FIELDS)$(id).value=w[key]||"";
 $("wlMaybe").checked=!!w.include_maybe;
 $("wlTelegram").textContent=!j.telegram
  ?"Telegram is not set up on this PC yet, so for now the watchlist only marks cars in New deals. To get a message on your phone for each one, add a bot token to data/secrets.toml (README, “Telegram alerts”)."
  :j.alerts_on?"Telegram is set up: you get a message for each new deal that fits."
  :"Telegram is set up, but deal alerts are switched off in config.toml ([alerts] deals).";
}
async function loadWatchlist(){
 try{
  const response=await fetch("/api/watchlist",{cache:"no-store"});
  const result=await response.json();
  if(!response.ok)throw Error(result.message||response.statusText);
  fillWatchlist(result);
 }catch(e){$("wlStatus").textContent="Could not load the watchlist: "+e;$("wlStatus").className="error";}
}
async function saveWatchlist(clear){
 const body={action:"save_watchlist",makes:clear?"":$("wlMakes").value,include_maybe:!clear&&$("wlMaybe").checked};
 for(const [id,key] of WL_FIELDS)body[key]=clear?0:(Number($(id).value)||0);
 $("saveWatchlist").disabled=$("clearWatchlist").disabled=true;
 try{
  const response=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const result=await response.json();
  $("wlStatus").textContent=result.message||"";
  $("wlStatus").className=response.ok?"ok":"error";
  if(response.ok){fillWatchlist(result);refresh();}
 }catch(e){$("wlStatus").textContent="Save failed: "+e;$("wlStatus").className="error";}
 finally{$("saveWatchlist").disabled=$("clearWatchlist").disabled=false;}
}
$("saveWatchlist").addEventListener("click",()=>saveWatchlist(false));
$("clearWatchlist").addEventListener("click",()=>saveWatchlist(true));
/* "Watchlist only" on the New deals list; remembered in this browser. */
let watchOnly=false;
try{watchOnly=localStorage.getItem("watchOnly")==="1";}catch(e){}
$("watchOnly").addEventListener("click",()=>{
 watchOnly=!watchOnly;
 try{localStorage.setItem("watchOnly",watchOnly?"1":"0");}catch(e){}
 refresh();
});

/* ---------- output: one pane per site ---------- */
const ERR=/traceback|error|failed|http (4|5)\d\d|refus|challenge|cut short|no response/i;
const TAG=/^(\s*)\[(\w+)\] /;
let SITES=[],lastLog="";
function buildPanes(sites){
 if(sites.join()===SITES.join())return;
 SITES=sites;lastLog="";
 $("panes").innerHTML=sites.map(s=>`<div class="pane" data-site="${esc(s)}">
  <div class="ph"><b>${esc(s)}</b><span class="st"></span></div><pre>Nothing yet.</pre></div>`).join("");
 /* Untick "follow" when you scroll up to read, re-tick at the bottom. */
 $("panes").querySelectorAll("pre").forEach(p=>p.addEventListener("scroll",()=>{
  $("follow").checked=p.scrollHeight-p.scrollTop-p.clientHeight<30;}));
}
/* A site's pane shows its own lines (tag removed) plus the lines no site
   owns: step headings, the report build. */
function renderLogs(lines,status,idle){
 const key=logSource+"\n"+lines.length+"\n"+(lines[lines.length-1]||"")+JSON.stringify(status||{});
 if(key===lastLog)return;lastLog=key;
 for(const pane of $("panes").children){
  const site=pane.dataset.site,pre=pane.querySelector("pre"),st=pane.querySelector(".st");
  const mine=[];
  for(const l of lines){
   const m=l.match(TAG);
   if(m&&SITES.includes(m[2])){if(m[2]===site)mine.push([l.replace(TAG,"$1"),true]);}
   else mine.push([l,false]);
  }
  pre.innerHTML=mine.length?mine.map(([l,own])=>{
   const cls=l.startsWith("▶")||l.startsWith("\n▶")?"s":own&&ERR.test(l)?"e":"";
   return cls?`<span class="${cls}">${esc(l)}</span>`:esc(l);
  }).join(""):esc(idle);
  if($("follow").checked)pre.scrollTop=pre.scrollHeight;
  const info=(status||{})[site];
  if(!info){st.textContent="no output yet";st.className="st";}
  else if(info.state==="blocked"){
   st.textContent=`problem ${ago(info.problem_at)}: ${info.problem.replace(/^!\s*/,"")}`;st.className="st bad";}
  else{st.textContent=`answering · last line ${ago(info.at)}`
   +(info.problem?` · earlier problem ${ago(info.problem_at)}`:"");st.className="st ok";}
 }
}

/* ---------- watcher health and deals ---------- */
const VERDICT={deal:"deal",maybe:"maybe",damaged:"damaged",problem:"problem"};
/* How the car compares with the similar cars in the database when it was found. */
function similarText(d){
 if(d.similar_n==null)return "";
 if(!d.similar_n)return " · no similar cars in the database yet";
 const t=`cheaper than ${d.similar_n-d.similar_cheaper} of ${d.similar_n} similar car${d.similar_n===1?"":"s"} (median €${fmt(d.similar_median)})`;
 return " · "+(d.verdict==="maybe"?t+" — too few to be sure":t);
}
/* "site|price|link" -> " · also on autoplius €8,900 ↗" */
function alsoText(also){
 if(!also)return "";
 const [src,price,link]=also.split("|");
 return ` · <a href="${esc(link)}" target="_blank" rel="noreferrer">also on ${esc(src)}${price?" €"+fmt(+price):""} ↗</a>`;
}
/* One health line per site: failing, stale, or its last scan. */
function siteHealth(src,s,running){
 if(!s||!s.last)return [`${src}: never run yet`,""];
 const l=s.last;
 if(s.failures)return [`${src}: failing — last ${s.failures} scan(s) could not read the site · last good scan ${ago(s.last_ok)}`,"bad"];
 if(running&&s.stale)return [`${src}: no successful scan for ${ago(s.last_ok).replace(" ago","")} — check the output`,"bad"];
 return [`${src}: last scan ${ago(l.started_at)} · ${l.new} new on ${l.pages} page(s)`
   +(l.hit_cap?" · hit the page cap, older ads may be missing":""),l.hit_cap?"bad":"ok"];
}
function renderWatcher(w,running){
 const h=$("watchHealth");
 const sites=Object.entries((w&&w.sites)||{});
 if(!sites.length){h.textContent="Never run yet.";h.className="d health";
  $("deals").innerHTML='<div class="empty">Cars the watcher finds under their price range appear here.</div>';return;}
 const lines=sites.map(([src,s])=>siteHealth(src,s,running));
 h.innerHTML=lines.map(([t,c])=>`<span class="${c}">${esc(t)}</span>`).join("<br>");
 h.className="d health";
 const only=watchOnly&&w.watchlist_set;
 $("watchOnly").hidden=!w.watchlist_set;$("watchOnly").classList.toggle("on",!!only);
 const deals=(only?w.watch_deals:w.deals)||[];
 $("dealsNote").textContent=deals.length?`${deals.filter(d=>d.verdict==="deal").length} confirmed deals of ${deals.length} ${only?"on your watchlist":"recent"}`:"";
 $("deals").innerHTML=deals.length?deals.map(d=>{
  const under=d.lo?Math.round((d.lo-d.price)/d.lo*100):null;
  const cls=VERDICT[d.verdict]||"other";
  return `<div class="deal"><span class="t">${d.watch?'<span class="star" title="Fits your watchlist">★</span> ':""}${esc(d.title)} ${d.year??""}</span>
   <span class="p">€${fmt(d.price)}</span><span class="v ${cls}">${esc(d.verdict)}</span>
   <span class="m">${esc(d.source)} · expected €${fmt(d.mid)} · ${under!=null?under+"% under the low end":""}${d.km?" · "+fmt(d.km)+" km":""}${
    similarText(d)}${d.flags?" · "+esc(d.flags.split("|").join(", ")):""}${alsoText(d.also)} · ${ago(d.found_at)} ·
    <a href="${esc(d.link)}" target="_blank" rel="noreferrer">open ↗</a></span></div>`;
 }).join(""):`<div class="empty">${only?"No recent deal fits your watchlist yet.":"No new car under its price range yet."}</div>`;
}
const notified=new Set();
async function refresh(){
 try{
  const response=await fetch("/api/status",{cache:"no-store"});
  if(!response.ok)throw Error(`HTTP ${response.status}`);
  const d=await response.json(),task=d.task,db=d.database,wt=d.watcher_task;
  const running=task?.status==="running", watching=wt?.status==="running";
  $("whoami").textContent=d.user?`${d.user.email} · ${d.user.role}`:"";

  $("chip").className="chip "+(running||watching?"running":task?.status==="failed"?"failed":"");
  $("chipText").textContent=(running?`${task.name} running · since ${new Date(task.started_at).toLocaleTimeString()}`
   :task?`No task running · last: ${task.name} ${task.status}`:"No task running")
   +(watching?" · watcher on":" · watcher off");
  $("stopButton").hidden=!running;
  $("logTitle").textContent=logSource==="job"?(task?`Output — ${task.name}`:"Output"):"Output — watcher";
  buildPanes(d.sites||[]);

  document.querySelectorAll(".task").forEach(row=>{
   const isWatcher=row.dataset.task==="watcher";
   const mine=isWatcher?watching:(running&&row.dataset.task===task.name);
   row.classList.toggle("running",mine);
   const b=row.querySelector("button");
   b.textContent=mine?"Stop":"Start";
   b.className=mine?"danger":b.dataset.action==="update"?"primary":"";
   b.dataset.running=mine?"1":"";
   b.disabled=!isWatcher&&running&&!mine;
  });
  $("audit").innerHTML=(d.audit||[]).length?d.audit.map(a=>`<div><b>${esc(a.action)}</b> ${
   esc(a.detail)} <span class="muted">· ${esc(a.email)} · ${ago(a.at)}</span></div>`).join("")
   :'<div class="muted">Nothing yet.</div>';

  $("sListings").textContent=fmt(db.listings);
  $("sModels").textContent=`${fmt(db.models_done)} / ${fmt(db.models_total)}`;
  $("sLast").textContent=ago(db.last_scraped);
  $("sDamaged").textContent=db.damaged==null?"—":fmt(db.damaged);
  $("sDamagedL").textContent=db.damage_checked
   ?`damaged · ${Math.round(db.damage_checked/Math.max(1,db.listings)*100)}% checked`:"damaged · not checked yet";
  const w=db.watcher;
  $("sNew").textContent=w?fmt(w.new_24h):"—";
  renderWatcher(w,watching);
  $("sReport").textContent=ago(db.report_built);
  const b=d.backup,bl=$("backupLine");
  bl.className="backupline"+(b&&b.error?" bad":"");
  bl.textContent=!b?"Backups are off (set [backup] dir in config.local.toml)."
   :b.error?`Backup failed: ${b.error}`
   :b.last?`Backup: ${ago(b.last)} to ${b.dir} · every ${b.every_days} days · ${b.count} kept`
   :`Backup: none yet · will be written to ${b.dir}`;
  if(logSource==="job")renderLogs(d.logs,task?.sites,"No task started yet.");
  else renderLogs(d.watcher_logs,wt?.sites,"The watcher has not printed anything yet.");

  for(const t of [task,wt].filter(Boolean)){const task=t;
   const id=task.started_at||task.name;
   if(task.error&&!notified.has(id+":e")){
    notified.add(id+":e");
    if(task.challenge_url)notify(`${new URL(task.challenge_url).hostname} refused a request`,"The refused page was opened in your browser. Wait a while, then restart the task — finished work is kept.","error",task.challenge_url);
    else notify(`${task.name}: error`,task.error);
   }
   if(["failed","completed","stopped"].includes(task.status)&&!notified.has(id+":f")){
    notified.add(id+":f");
    if(task.status==="failed"&&!task.error)notify(`${task.name} failed`,`Exited with code ${task.exit_code}. See the output.`);
    else if(task.status==="stopped")notify(`${task.name} stopped`,"Stopped from the dashboard.","warning");
    else if(task.status==="completed"&&task.name==="watcher")notify("Watcher stopped unexpectedly","Start it again to resume its scans.");
    else if(task.status==="completed"){notify(`${task.name} finished`,"Completed successfully.","success");reportLoaded=false;}
   }
  }
 }catch(e){
  $("chip").className="chip failed";$("chipText").textContent="Dashboard not reachable";
 }
}
showView(location.hash.slice(1));
window.addEventListener("hashchange",()=>showView(location.hash.slice(1)));
loadModels();loadWatchlist();refresh();setInterval(refresh,2000);
</script></body></html>"""


AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    at     TEXT,
    email  TEXT,
    action TEXT,
    detail TEXT
);
"""


def audit(email, action, detail=""):
    """Record who did what: started or stopped a task, labelled a car,
    changed the model list."""
    connection = sqlite3.connect(DB_PATH, timeout=10)
    try:
        connection.executescript(AUDIT_SCHEMA)
        connection.execute("INSERT INTO audit VALUES (?,?,?,?)",
                           (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            email, action, str(detail)[:300]))
        connection.commit()
    finally:
        connection.close()


def recent_audit(limit=15):
    if not DB_PATH.exists():
        return []
    connection = sqlite3.connect(DB_PATH, timeout=5)
    try:
        connection.executescript(AUDIT_SCHEMA)
        return [dict(zip(("at", "email", "action", "detail"), row)) for row in
                connection.execute(
                    "SELECT * FROM audit ORDER BY at DESC, rowid DESC LIMIT ?", (limit,))]
    finally:
        connection.close()


AD_ID_RE = re.compile(r"[0-9A-Za-z-]{1,40}")


def save_label(payload):
    """Your verdict on one car, from the report or the New deals list:
    {"source": "autoplius", "id": "32908972", "label": "deal" | "damaged" |
    "fake" | ""}. An empty label removes yours."""
    source, ad_id, label = payload.get("source"), payload.get("id"), payload.get("label")
    if source not in SITES:
        raise ValueError("Unknown site.")
    if not isinstance(ad_id, str) or not AD_ID_RE.fullmatch(ad_id):
        raise ValueError("Invalid listing id.")
    if label not in (*LABELS, ""):
        raise ValueError("Unknown label.")
    connection = sqlite3.connect(DB_PATH, timeout=10)
    try:
        set_label(connection, source, ad_id, label)
    finally:
        connection.close()
    return f"Marked as {LABELS[label]}." if label else "Label removed."


def watchlist_state(wanted=None):
    """The watchlist as the Watchlist page shows it, and whether alerts can
    actually be sent."""
    if wanted is None:
        wanted = dict(watchlist.EMPTY)
        if DB_PATH.exists():
            connection = sqlite3.connect(DB_PATH, timeout=5)
            try:
                wanted = watchlist.load(connection)
            finally:
                connection.close()
    return {"watchlist": wanted, "telegram": alerts.telegram_config() is not None,
            "alerts_on": SETTINGS["alerts"]["deals"]}


def save_watchlist(payload):
    """Check and save the watchlist from the Watchlist page, and mark the
    deals already listed against it. Returns (watchlist, message)."""
    connection = sqlite3.connect(DB_PATH, timeout=10)
    try:
        makes, pairs = watchlist.known_names(connection)
        wanted = watchlist.parse(payload, makes, pairs)
        watchlist.save(connection, wanted)
        ensure_watch_tables(connection)
        fits = watchlist.reflag(connection, wanted, SETTINGS["watcher"]["min_similar"])
    finally:
        connection.close()
    if not watchlist.is_set(wanted):
        return wanted, "Watchlist cleared: every confirmed deal counts again."
    return wanted, (f"Saved: {watchlist.describe(wanted)}. "
                    f"{fits} of the deals already listed fit it.")


def save_database_models(text):
    """
    Choose which catalogue models the database scraper covers. Listed models
    are enabled, all others switched off. Nothing is deleted: a switched-off
    model keeps its listings and history, and the weekly catalogue sync leaves
    it off.
    """
    if len(text.encode("utf-8")) > 200_000:
        raise ValueError("Model list is too large (maximum 200 KB).")
    wanted = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip().lower() for part in line.split("/")]
        if len(parts) != 2 or not all(re.fullmatch(r"[\w+-]{1,100}", p) for p in parts):
            raise ValueError(f"Line {line_number}: expected make/model, e.g. audi/a3.")
        if tuple(parts) not in wanted:
            wanted.append(tuple(parts))
    if not wanted:
        raise ValueError("Keep at least one make/model in the list.")

    store = Store(DB_PATH)
    try:
        with store.lock:
            known = {(mk, md) for mk, md in store.conn.execute(
                "SELECT make, model FROM models WHERE source = 'autoplius'")}
            unknown = [f"{mk}/{md}" for mk, md in wanted if (mk, md) not in known]
            if unknown:
                raise ValueError(
                    "Not in the Autoplius catalogue: " + ", ".join(unknown[:10])
                    + (f" and {len(unknown) - 10} more" if len(unknown) > 10 else "")
                    + ". Run 'Update database' once to sync the catalogue.")
            before = dict(((mk, md), en) for mk, md, en in store.conn.execute(
                """SELECT make, model, MAX(enabled) FROM models
                   WHERE source = 'autoplius' GROUP BY make, model"""))
            store.conn.execute("UPDATE models SET enabled = 0 WHERE source = 'autoplius'")
            store.conn.executemany(
                """UPDATE models SET enabled = 1
                   WHERE source = 'autoplius' AND make = ? AND model = ?""", wanted)
            store.conn.commit()
    finally:
        store.close()
    keep = set(wanted)
    added = sum(1 for pair in keep if not before.get(pair))
    removed = sum(1 for pair, on in before.items() if on and pair not in keep)
    saved_text = "\n".join(f"{mk}/{md}" for mk, md in sorted(keep)) + "\n"
    message = (f"{len(keep)} models enabled: {added} switched on, {removed} switched off. "
               "Listings already in the database were kept.")
    return saved_text, len(keep), message


class JobManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.name = None
        self.status = None
        self.started_at = None
        self.finished_at = None
        self.logs = deque(maxlen=1000)
        self.error = None
        self.exit_code = None
        self.stop_requested = False
        self.challenge_url = None
        self.challenge_opened = False
        self.sites = {}

    def _track_site(self, text):
        """Keep, per site, whether it is answering: {"state": "ok" | "blocked",
        "problem": the line that said no, "problem_at", "last", "at"}. Two sites
        run side by side, so "HTTP 403" has to be pinned on the one that got it."""
        match = SITE_LINE_RE.match(text)
        if not match or match.group(1) not in SITES or not match.group(2).strip():
            return
        line = match.group(2).strip()[:300]
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        info = self.sites.setdefault(match.group(1), {
            "state": "ok", "problem": None, "problem_at": None, "clean": 0})
        info["last"], info["at"] = line, now
        if PROBLEM_RE.search(line):
            info.update(state="blocked", problem=line, problem_at=now, clean=0)
        elif info["state"] == "blocked":
            info["clean"] += 1
            if WORKING_RE.search(line) or info["clean"] >= CLEAR_AFTER:
                info["state"] = "ok"

    def append_log(self, text):
        with self.lock:
            self.logs.append(text)
            self._track_site(text)
            lowered = text.lower()
            if self.error is None and any(marker in lowered for marker in (
                "traceback (most recent call last)",
                "database import failed:",
                "could not build a model list.",
                "stopped: the site refused",
                "http 403",
                "http 429",
                "http 500",
                "http 502",
                "http 503",
                "http 504",
                "verification challenge",
                "description request failed",
            )):
                self.error = text.strip()[:500]

    def open_refused_page(self, text):
        match = re.search(
            r"(?:\bHTTP 403|verification challenge) for (https://[^\s]+)", text
        )
        if not match:
            return
        url = match.group(1).rstrip(".,)")
        host = urlparse(url).hostname
        if host not in REFUSED_HOSTS:
            return
        with self.lock:
            if self.challenge_opened:
                return
            self.challenge_opened = True
            self.challenge_url = url
        try:
            opened = webbrowser.open(url, new=2)
        except webbrowser.Error as exc:
            self.append_log(f"Could not open the refused page in a browser: {exc}\n")
        else:
            if opened:
                self.append_log(
                    f"{host} refused or challenged a request. Opened the page in your "
                    "browser for manual review; browser verification is not transferred "
                    "to the scraper.\n"
                )
            else:
                self.append_log(
                    f"{host} refused or challenged a request. The browser could not "
                    "be opened automatically; "
                    f"use the dashboard link: {url}\n"
                )

    def start(self, action):
        name, steps = TASKS[action]
        command, rest = steps[0], steps[1:]
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return False, f"{self.name} is already running. Stop it before starting another task."
            self.logs.clear()
            self.name = name
            self.status = "starting"
            self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.finished_at = None
            self.error = None
            self.exit_code = None
            self.stop_requested = False
            self.challenge_url = None
            self.challenge_opened = False
            self.sites = {}
            self.process = _spawn(command)
            process = self.process
            self.status = "running"
            if rest:
                self.logs.append(f"▶ step 1 of {len(steps)}: {_describe(command)}\n")
        threading.Thread(
            target=self._collect_output, args=(process, name, rest), daemon=True,
        ).start()
        return True, f"Started {name}."

    def _pump(self, process, name):
        for line in process.stdout:
            self.append_log(line)
            self.open_refused_page(line)
        return process.wait()

    def _collect_output(self, process, name, rest=()):
        try:
            return_code = self._pump(process, name)
            # Multi-step tasks: each step runs only if the previous one succeeded
            # and nobody pressed Stop.
            for number, command in enumerate(rest, start=2):
                with self.lock:
                    if return_code != 0 or self.stop_requested or self.process is not process:
                        break
                    self.logs.append(
                        f"\n▶ step {number} of {len(rest) + 1}: {_describe(command)}\n")
                    process = self.process = _spawn(command)
                return_code = self._pump(process, name)
        except Exception as exc:
            self.append_log(f"Task output error: {type(exc).__name__}: {exc}\n")
            with self.lock:
                self.error = f"{type(exc).__name__}: {exc}"[:500]
            return_code = process.poll()
        with self.lock:
            if self.process is process:
                self.exit_code = return_code
                self.status = "running" if return_code is None else (
                    "stopped" if self.stop_requested
                    else "completed" if return_code == 0
                    else "failed"
                )
                self.finished_at = (
                    None if return_code is None
                    else datetime.now().astimezone().isoformat(timespec="seconds")
                )
                if return_code not in (None, 0):
                    self.logs.append(f"Task exited with code {return_code}.\n")

    def stop(self):
        with self.lock:
            process = self.process
            name = self.name
            if process is not None and process.poll() is None:
                self.stop_requested = True
        if process is None or process.poll() is not None:
            return False, "No task is running."
        try:
            ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
            if ctrl_break is not None:
                process.send_signal(ctrl_break)
            else:
                process.terminate()
            process.wait(timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()
        self.append_log(f"Stopped {name} from the dashboard.\n")
        return True, f"Stopped {name}."

    def snapshot(self):
        with self.lock:
            task = None
            if self.process is not None:
                task = {
                    "name": self.name,
                    "status": self.status,
                    "started_at": self.started_at,
                    "finished_at": self.finished_at,
                    "error": self.error,
                    "exit_code": self.exit_code,
                    "challenge_url": self.challenge_url,
                    "sites": {name: dict(info) for name, info in self.sites.items()},
                }
            return task, list(self.logs)


def database_summary():
    result = {"listings": 0, "models_done": 0, "models_total": 0,
              "watcher": None, "last_scraped": None,
              "damaged": None, "damage_checked": None, "power_known": None,
              "descriptions": None, "candidates": None, "report_built": None}
    if REPORT_PATH.exists():
        result["report_built"] = datetime.fromtimestamp(
            REPORT_PATH.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    if CANDIDATES_PATH.exists():
        try:
            lines = CANDIDATES_PATH.read_text(encoding="utf-8").splitlines()
            result["candidates"] = sum(1 for l in lines if l.strip() and not l.startswith("#"))
        except OSError:
            pass
    if not DB_PATH.exists():
        return result
    connection = sqlite3.connect(DB_PATH, timeout=2)
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "listings" in tables:
            result["listings"] = connection.execute(
                "SELECT COUNT(*) FROM listings"
            ).fetchone()[0]
            columns = {row[1] for row in connection.execute("PRAGMA table_info(listings)")}
            if {"damaged", "power_kw", "description_checked_at"} <= columns:
                (result["damaged"], result["damage_checked"], result["power_known"],
                 result["descriptions"]) = connection.execute(
                    """SELECT SUM(damaged = 1), COUNT(damaged), COUNT(power_kw),
                              COUNT(description_checked_at) FROM listings""").fetchone()
        if "models" in tables:
            result["models_total"], result["models_done"], result["last_scraped"] = \
                connection.execute(
                    """SELECT COUNT(*), COUNT(last_scraped), MAX(last_scraped) FROM models"""
                ).fetchone()
        if "watch_scans" in tables:
            sites = {site.SOURCE: watcher_health(connection, site.SOURCE)
                     for site in enabled_sites()}
            deals = sorted((d for h in sites.values() if h for d in h["deals"]),
                           key=lambda d: d["found_at"], reverse=True)[:40]
            watched = sorted((d for h in sites.values() if h for d in h["watch_deals"]),
                             key=lambda d: d["found_at"], reverse=True)[:40]
            result["watcher"] = {
                "sites": sites, "deals": deals, "watch_deals": watched,
                "watchlist_set": watchlist.is_set(watchlist.load(connection)),
                "new_24h": sum(h["new_24h"] for h in sites.values() if h),
            }
    finally:
        connection.close()
    return result
