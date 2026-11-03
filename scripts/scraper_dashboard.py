#!/usr/bin/env python3
"""Local web controls for the Autoplius scrapers and price report."""

import argparse
import json
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from collectors import enabled_sites
from collectors.store import Store
from collectors.watch import health as watcher_health

from settings import SETTINGS, project_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
DATA_DIR = project_path(SETTINGS["paths"]["data_dir"])
DB_PATH = project_path(SETTINGS["paths"]["database"])
REPORT_PATH = project_path(SETTINGS["paths"]["reports_dir"]) / "report.html"
REPORT_LOCK = threading.Lock()
CANDIDATES_PATH = DATA_DIR / "candidates.txt"


def _script(name, *args):
    return [sys.executable, "-u", str(SCRIPTS / name), *args]


# action -> (task name shown in the dashboard, [command, command, ...]).
# Commands run one after another; a failed or stopped step ends the task.
TASKS = {
    "start_watcher": ("watcher", [
        _script("watch.py")]),
    "start_database": ("database scraper", [_script("scrape.py")]),
    "rescrape_all": ("full re-scrape", [_script("scrape.py", "--force")]),
    "refresh_damage": ("damage check", [_script("scrape.py", "--damage-sweep")]),
    "check_candidates": ("candidate check", [
        _script("autoplius_stats.py"),
        _script("scrape.py", "--backfill-descriptions",
                "--ids-file", str(CANDIDATES_PATH)),
        _script("autoplius_stats.py"),
    ]),
    "build_report": ("report build", [_script("autoplius_stats.py")]),
}


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
<title>Autoplius dashboard</title>
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
.right{min-height:0;display:flex;flex-direction:column;gap:12px}
.log{flex:1;min-height:0;display:flex;flex-direction:column}
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
 .log{height:60vh;flex:none}
 .report{height:calc(100vh - 100px)}
 .stats{grid-template-columns:repeat(2,1fr)}
 .stat:nth-child(3n){border-right:1px solid var(--line)}.stat:nth-child(2n){border-right:0}
 .stat:nth-last-child(-n+3){border-bottom:1px solid var(--line)}.stat:nth-last-child(-n+2){border-bottom:0}
}
</style></head>
<body>
<header class="top">
 <span class="brand">Autoplius</span>
 <nav class="nav" aria-label="Sections">
  <button data-view="control" aria-current="page">Control</button>
  <button data-view="report">Deals &amp; prices</button>
  <button data-view="models">Models</button>
 </nav>
 <span class="spacer"></span>
 <span class="bar" id="reportTools" hidden><button id="reloadReport" title="Rebuild if the database changed, then reload">Reload report</button>
  <a href="/report" target="_blank" rel="noreferrer">Open in new tab</a></span>
 <span class="chip" id="chip"><span class="dot"></span><span id="chipText">Connecting…</span></span>
 <button class="danger" id="stopButton" hidden>Stop</button>
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
    <div class="stat"><div class="v" id="sPower">—</div><div class="l">have kW / gearbox</div></div>
    <div class="stat"><div class="v" id="sDesc">—</div><div class="l">descriptions read</div></div>
    <div class="stat"><div class="v" id="sCand">—</div><div class="l">candidates to read</div></div>
    <div class="stat"><div class="v" id="sNew">—</div><div class="l">new ads, last 24 h</div></div>
    <div class="stat"><div class="v" id="sReport">—</div><div class="l">report built</div></div>
   </div>
  </div>
  <div class="card">
   <h2>Tasks <span class="spacer"></span><span id="notice" style="text-transform:none;letter-spacing:0"></span></h2>
   <div class="group-label">Live</div>
   <div class="task" data-task="watcher"><div class="t"><div class="n">New-listing watcher</div>
    <div class="d">Reads each site's newest ads every 5 minutes until it reaches ones it already has, prices them and checks cheap ones for defects.</div>
    <div class="d health" id="watchHealth"></div></div>
    <button data-action="start_watcher">Start</button></div>
   <div class="group-label">Collect</div>
   <div class="task" data-task="database scraper"><div class="t"><div class="n">Resume scraping</div>
    <div class="d">Every model (Autoplius) or make (autogidas) not scraped in the last 7 days, then each site's damage sweep.</div></div>
    <button data-action="start_database">Start</button></div>
   <div class="task" data-task="full re-scrape"><div class="t"><div class="n">Full re-scrape</div>
    <div class="d">Every model again, regardless of age — refreshes every price and car detail. About 4 hours at a polite pace.</div></div>
    <button data-action="rescrape_all" data-confirm="Re-scrape every model? This takes about 4 hours.">Start</button></div>
   <div class="group-label">Clean up</div>
   <div class="task" data-task="damage check"><div class="t"><div class="n">Flag damaged cars</div>
    <div class="d">Each site's own defect filter (Autoplius ~120 pages, autogidas ~40 pages). ~5–10 min.</div></div>
    <button data-action="refresh_damage">Start</button></div>
   <div class="task" data-task="candidate check"><div class="t"><div class="n">Check top candidates</div>
    <div class="d">Rebuilds the deal list, reads the 300 best unread ads for defects, rebuilds again. ~15 min.</div></div>
    <button data-action="check_candidates">Start</button></div>
   <div class="group-label">Report</div>
   <div class="task" data-task="report build"><div class="t"><div class="n">Rebuild report</div>
    <div class="d">Refits the price model and refreshes the deal list. ~30 s.</div></div>
    <button data-action="build_report">Start</button></div>
   <div style="height:6px"></div>
  </div>
  <p style="margin:0;font-size:12px;color:var(--muted)">One task runs at a time. Stopping is safe: finished pages are saved and the next run resumes. If Autoplius refuses requests, the refused page opens in your browser — wait before resuming.</p>
 </div>
 <div class="right">
 <div class="card deals">
  <h2>New deals <span class="spacer"></span><span id="dealsNote" style="text-transform:none;letter-spacing:0"></span></h2>
  <div class="list" id="deals"><div class="empty">Cars the watcher finds under their price range appear here.</div></div>
 </div>
 <div class="card log">
  <h2><span id="logTitle">Output</span><span class="spacer"></span>
   <label><input type="checkbox" id="follow" checked> Follow output</label></h2>
  <pre id="logs">No task started yet.</pre>
 </div>
 </div>
</section>

<section class="view report" id="v-report" hidden>
  <iframe id="reportFrame" title="Deals and price analysis"></iframe>
 <div class="overlay" id="reportOverlay"><strong>Loading report…</strong>
  <span>If the database changed since the last build, the price model is refitted first — about 30 seconds.</span></div>
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
 <a id="alertLink" target="_blank" rel="noreferrer" hidden>Open the refused Autoplius page</a></aside>

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
function showView(view){
 if(!["control","report","models"].includes(view))view="control";
 document.querySelectorAll(".nav button").forEach(b=>b.toggleAttribute("aria-current",b.dataset.view===view));
 document.querySelectorAll(".nav button[aria-current]").forEach(b=>b.setAttribute("aria-current","page"));
 ["control","report","models"].forEach(v=>$("v-"+v).hidden=v!==view);
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
 const action=b.dataset.running?"stop":b.dataset.action;
 if(action!=="stop"&&b.dataset.confirm&&!confirm(b.dataset.confirm))return;
 act(action);
}));
$("stopButton").addEventListener("click",()=>act("stop"));

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

/* ---------- status ---------- */
const ERR=/traceback|error|failed|http (4|5)\d\d|refus|challenge/i;
let lastLog="";
function renderLog(lines){
 const text=lines.join("");
 if(text===lastLog)return;lastLog=text;
 const pre=$("logs");
 pre.innerHTML=lines.length?lines.map(l=>{
  const cls=l.startsWith("▶")||l.startsWith("\n▶")?"s":ERR.test(l)?"e":"";
  return cls?`<span class="${cls}">${esc(l)}</span>`:esc(l);
 }).join(""):"No task started yet.";
 if($("follow").checked)pre.scrollTop=pre.scrollHeight;
}
/* Untick "follow" automatically when you scroll up to read, re-tick at the bottom. */
$("logs").addEventListener("scroll",()=>{
 const p=$("logs");$("follow").checked=p.scrollHeight-p.scrollTop-p.clientHeight<30;
});

/* ---------- watcher health and deals ---------- */
const VERDICT={deal:"deal",damaged:"damaged",problem:"problem"};
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
 const deals=w.deals||[];
 $("dealsNote").textContent=deals.length?`${deals.filter(d=>d.verdict==="deal").length} deals of ${deals.length} recent`:"";
 $("deals").innerHTML=deals.length?deals.map(d=>{
  const under=d.lo?Math.round((d.lo-d.price)/d.lo*100):null;
  const cls=VERDICT[d.verdict]||"other";
  return `<div class="deal"><span class="t">${esc(d.title)} ${d.year??""}</span>
   <span class="p">€${fmt(d.price)}</span><span class="v ${cls}">${esc(d.verdict)}</span>
   <span class="m">${esc(d.source)} · expected €${fmt(d.mid)} · ${under!=null?under+"% under the low end":""}${d.km?" · "+fmt(d.km)+" km":""}${
    d.flags?" · "+esc(d.flags.split("|").join(", ")):""} · ${ago(d.found_at)} ·
    <a href="${esc(d.link)}" target="_blank" rel="noreferrer">open ↗</a></span></div>`;
 }).join(""):'<div class="empty">No new car under its price range yet.</div>';
}

const notified=new Set();
async function refresh(){
 try{
  const response=await fetch("/api/status",{cache:"no-store"});
  if(!response.ok)throw Error(`HTTP ${response.status}`);
  const d=await response.json(),task=d.task,db=d.database;
  const running=task?.status==="running";

  $("chip").className="chip "+(running?"running":task?.status==="failed"?"failed":"");
  $("chipText").textContent=running?`${task.name} running · since ${new Date(task.started_at).toLocaleTimeString()}`
   :task?`Idle · last: ${task.name} ${task.status}`:"Idle";
  $("stopButton").hidden=!running;
  $("logTitle").textContent=task?`Output — ${task.name}`:"Output";

  document.querySelectorAll(".task").forEach(row=>{
   const mine=running&&row.dataset.task===task.name;
   row.classList.toggle("running",mine);
   const b=row.querySelector("button");
   b.textContent=mine?"Stop":"Start";
   b.className=mine?"danger":"";
   b.dataset.running=mine?"1":"";
   b.disabled=running&&!mine;
  });

  $("sListings").textContent=fmt(db.listings);
  $("sModels").textContent=`${fmt(db.models_done)} / ${fmt(db.models_total)}`;
  $("sLast").textContent=ago(db.last_scraped);
  $("sDamaged").textContent=db.damaged==null?"—":fmt(db.damaged);
  $("sDamagedL").textContent=db.damage_checked
   ?`damaged · ${Math.round(db.damage_checked/Math.max(1,db.listings)*100)}% checked`:"damaged · not checked yet";
  $("sPower").textContent=db.power_known==null?"—":Math.round(db.power_known/Math.max(1,db.listings)*100)+"%";
  $("sDesc").textContent=fmt(db.descriptions);
  $("sCand").textContent=fmt(db.candidates);
  const w=db.watcher;
  $("sNew").textContent=w?fmt(w.new_24h):"—";
  renderWatcher(w,running&&task.name==="watcher");
  $("sReport").textContent=ago(db.report_built);
  renderLog(d.logs);

  if(task){
   const id=task.started_at||task.name;
   if(task.error&&!notified.has(id+":e")){
    notified.add(id+":e");
    if(task.challenge_url)notify("Autoplius refused a request","The refused page was opened in your browser. Wait a while, then restart the task — finished work is kept.","error",task.challenge_url);
    else notify(`${task.name}: error`,task.error);
   }
   if(["failed","completed","stopped"].includes(task.status)&&!notified.has(id+":f")){
    notified.add(id+":f");
    if(task.status==="failed"&&!task.error)notify(`${task.name} failed`,`Exited with code ${task.exit_code}. See the output.`);
    else if(task.status==="stopped")notify(`${task.name} stopped`,"Stopped from the dashboard.","warning");
    else if(task.status==="completed"&&task.name==="watcher")notify("Watcher stopped unexpectedly","Start it again to resume five-minute scans.");
    else if(task.status==="completed"){notify(`${task.name} finished`,"Completed successfully.","success");reportLoaded=false;}
   }
  }
 }catch(e){
  $("chip").className="chip failed";$("chipText").textContent="Dashboard not reachable";
 }
}
showView(location.hash.slice(1));
window.addEventListener("hashchange",()=>showView(location.hash.slice(1)));
loadModels();refresh();setInterval(refresh,2000);
</script></body></html>"""


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
                    + ". Run 'Resume scraping' once to sync the catalogue.")
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
        self.logs = deque(maxlen=400)
        self.error = None
        self.exit_code = None
        self.stop_requested = False
        self.challenge_url = None
        self.challenge_opened = False

    def append_log(self, text):
        with self.lock:
            self.logs.append(text)
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
        if urlparse(url).hostname != "autoplius.lt":
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
                    "Autoplius refused or challenged a request. Opened the page in your "
                    "browser for manual review; browser verification is not transferred "
                    "to the scraper.\n"
                )
            else:
                self.append_log(
                    "Autoplius refused or challenged a request. The browser could not "
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
            result["watcher"] = {
                "sites": sites, "deals": deals,
                "new_24h": sum(h["new_24h"] for h in sites.values() if h),
            }
    finally:
        connection.close()
    return result


def make_handler(manager):
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            route = urlparse(self.path).path
            if route == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif route == "/api/status":
                task, logs = manager.snapshot()
                self._send_json(200, {
                    "task": task,
                    "logs": logs[-150:],
                    "database": database_summary(),
                    "report_exists": REPORT_PATH.exists(),
                })
            elif route == "/api/models":
                if not DB_PATH.exists():
                    self._send_json(200, {
                        "text": "",
                        "count": 0,
                        "message": "Run or initialize the database scraper to load models.",
                    })
                    return
                connection = sqlite3.connect(DB_PATH, timeout=5)
                try:
                    rows = connection.execute(
                        """SELECT DISTINCT make, model FROM models
                           WHERE source = 'autoplius' AND enabled = 1
                           ORDER BY make, model"""
                    ).fetchall()
                except sqlite3.Error as exc:
                    self._send_json(500, {
                        "message": f"Could not read the database scraper model inventory: {exc}",
                    })
                    return
                finally:
                    connection.close()
                text = "\n".join(f"{make}/{model}" for make, model in rows)
                self._send_json(200, {"text": text, "count": len(rows)})
            elif route == "/report":
                if not DB_PATH.exists():
                    self.send_error(404, "Run a scraper to create the price database first.")
                    return
                try:
                    with REPORT_LOCK:
                        database_updated = DB_PATH.stat().st_mtime_ns
                        report_updated = (
                            REPORT_PATH.stat().st_mtime_ns
                            if REPORT_PATH.exists() else 0
                        )
                        if database_updated > report_updated:
                            result = subprocess.run(
                                [
                                    sys.executable, "-u",
                                    str(SCRIPTS / "autoplius_stats.py"),
                                ],
                                cwd=PROJECT_ROOT,
                                capture_output=True,
                                text=True,
                                encoding="utf-8",
                                errors="replace",
                                timeout=300,
                                check=False,
                            )
                            if result.returncode != 0:
                                detail = result.stderr.strip() or result.stdout.strip()
                                self.send_error(
                                    500,
                                    "Could not update the price report. "
                                    + (detail[-1000:] if detail else
                                       f"Report process exited with code {result.returncode}."),
                                )
                                return
                        body = REPORT_PATH.read_bytes()
                except (OSError, subprocess.TimeoutExpired) as exc:
                    self.send_error(
                        500, f"Could not update or read the price report: {exc}"
                    )
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def do_POST(self):
            host = self.headers.get("Host", "").split(":")[0].lower()
            if host not in {"127.0.0.1", "localhost"}:
                self._send_json(403, {"ok": False, "message": "Local requests only."})
                return
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).hostname not in {"127.0.0.1", "localhost"}:
                self._send_json(403, {"ok": False, "message": "Local requests only."})
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self._send_json(415, {"ok": False, "message": "Expected JSON request."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 65536:
                    raise ValueError("Request body must be between 1 and 65536 bytes.")
                payload = json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"ok": False, "message": str(exc)})
                return

            if not isinstance(payload, dict):
                self._send_json(400, {"ok": False, "message": "Expected a JSON object."})
                return
            action = payload.get("action")
            if action == "save_database_models":
                text = payload.get("text")
                if not isinstance(text, str):
                    self._send_json(400, {
                        "ok": False,
                        "message": "Model inventory must be text.",
                    })
                    return
                try:
                    text, count, message = save_database_models(text)
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "message": str(exc)})
                    return
                except (OSError, sqlite3.Error) as exc:
                    self._send_json(500, {
                        "ok": False,
                        "message": f"Could not update database scraper models: {exc}",
                    })
                    return
                self._send_json(200, {
                    "ok": True,
                    "message": message,
                    "text": text,
                    "count": count,
                })
                return
            if action == "stop":
                ok, message = manager.stop()
                self._send_json(200 if ok else 409, {"ok": ok, "message": message})
                return
            if action not in TASKS:
                self._send_json(400, {"ok": False, "message": "Unknown action."})
                return
            try:
                ok, message = manager.start(action)
            except OSError as exc:
                self._send_json(500, {
                    "ok": False,
                    "message": f"Could not start task: {type(exc).__name__}: {exc}",
                })
                return
            self._send_json(202 if ok else 409, {"ok": ok, "message": message})

        def log_message(self, _format, *_args):
            del _format, _args
            return

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Run the local scraper controls dashboard.")
    parser.add_argument("--port", type=int, default=SETTINGS["dashboard"]["port"])
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-watcher", action="store_true",
                        default=not SETTINGS["dashboard"]["start_watcher"],
                        help="do not start the five-minute watcher on launch")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    manager = JobManager()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(manager))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Scraper dashboard: {url}", flush=True)
    print("Bound to localhost only. Ctrl+C stops the dashboard and active task.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    if not args.no_watcher:
        print(manager.start("start_watcher")[1], flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping dashboard and active task…", flush=True)
    finally:
        active, _, _ = manager.snapshot()
        if active and active["status"] == "running":
            manager.stop()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
