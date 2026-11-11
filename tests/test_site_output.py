"""Whose output is it? Lines tagged per site, and the dashboard's per-site status."""

import io
import sys

from collectors import common
from web import admin


def test_site_output_tags_every_line(monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    print("before")
    with common.site_output("autogidas"):
        print("[1/92] bmw/*")
        print("  ! HTTP 403 for https://autogidas.lt/x", file=sys.stderr)
        print("\ntwo\nlines")
        sys.stdout.write("half ")
        sys.stdout.write("line\n")
    print("after")
    assert out.getvalue() == ("before\n[autogidas] [1/92] bmw/*\n\n[autogidas] two\n"
                              "[autogidas] lines\n[autogidas] half line\nafter\n")
    assert err.getvalue() == "[autogidas]   ! HTTP 403 for https://autogidas.lt/x\n"


def test_refusal_kind_names_a_cloudflare_challenge():
    assert "Cloudflare browser challenge" in common.refusal_kind(
        "<title>Just a moment...</title> Enable JavaScript and cookies to continue")
    assert "CAPTCHA" in common.refusal_kind('<div class="g-recaptcha"></div>')


def test_dashboard_pins_a_refusal_on_the_site_that_got_it():
    jobs = admin.JobManager()
    jobs.append_log("▶ step 1 of 4: scraping autoplius and autogidas\n")
    jobs.append_log("[autoplius] [1/500] bmw/320\n")
    jobs.append_log("[autoplius]     17 listing(s) of 17\n")
    jobs.append_log("[autogidas]   ! HTTP 403 for https://autogidas.lt/skelbimai/ — "
                    "a Cloudflare browser challenge (JavaScript check)\n")
    jobs.append_log("[autogidas]     page 1: no response — stopping\n")
    assert jobs.sites["autoplius"]["state"] == "ok"
    assert jobs.sites["autoplius"]["problem"] is None
    blocked = jobs.sites["autogidas"]
    assert blocked["state"] == "blocked" and "no response" in blocked["problem"]
    # an unrelated next line doesn't clear it; a page that worked does
    jobs.append_log("[autogidas] [2/92] audi/*\n")
    assert jobs.sites["autogidas"]["state"] == "blocked"
    jobs.append_log("[autogidas]     240 listing(s) of 240\n")
    assert jobs.sites["autogidas"]["state"] == "ok"
    assert "no response" in jobs.sites["autogidas"]["problem"]      # still shown as earlier


def test_watcher_scan_lines_set_the_site_status():
    watcher = admin.JobManager()
    watcher.append_log("[autoplius] [21:00:02] No results page could be read (HTTP 403, "
                       "a Cloudflare browser challenge (JavaScript check)); nothing changed.\n")
    assert watcher.sites["autoplius"]["state"] == "blocked"
    watcher.append_log("[autoplius] [21:03:02] 0 new ad(s) on 2 page(s)\n")
    assert watcher.sites["autoplius"]["state"] == "ok"


def test_every_task_runs_a_script_that_exists():
    for _name, steps in admin.TASKS.values():
        for command in steps:
            assert admin.Path(command[2]).exists(), command
    assert set(admin.TASKS) == {"start_watcher", "update", "rescrape_all", "build_report"}
