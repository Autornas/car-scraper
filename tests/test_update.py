"""update.py: every site scraped side by side, each line tagged with its site."""

import sys

import update

FAKE_SCRAPE = '''import sys
site = sys.argv[sys.argv.index("--site") + 1]
print(f"[{site}] args " + " ".join(sys.argv[1:]))
print("summary line without a tag")
sys.exit(3 if site == "autogidas" and "--backfill-descriptions" not in sys.argv else 0)
'''


def run(tmp_path, monkeypatch, capsys, *argv):
    (tmp_path / "scrape.py").write_text(FAKE_SCRAPE, encoding="utf-8")
    (tmp_path / "autoplius_stats.py").write_text("print('report built')", encoding="utf-8")
    candidates = tmp_path / "candidates.txt"
    candidates.write_text("autoplius:1\n", encoding="utf-8")
    monkeypatch.setattr(update, "SCRIPTS", tmp_path)
    monkeypatch.setattr(update, "CANDIDATES", candidates)
    monkeypatch.setattr(update, "catalogue_missing", lambda path: False)
    monkeypatch.setattr(update, "LOCK_PATH", tmp_path / "update.lock")
    monkeypatch.setattr(sys, "argv", ["update.py", "--site", "autoplius", "--site", "autogidas",
                                      *argv])
    code = update.main()
    return code, capsys.readouterr().out.splitlines()


def test_update_runs_the_sites_together_and_tags_their_lines(tmp_path, monkeypatch, capsys):
    code, lines = run(tmp_path, monkeypatch, capsys, "--reads", "7")
    assert "[autoplius] args --site autoplius" in lines
    assert "[autogidas] args --site autogidas" in lines
    # a line the scraper printed without a tag is still pinned on its site
    assert "[autoplius] summary line without a tag" in lines
    assert "[autogidas] summary line without a tag" in lines
    # the report is rebuilt twice, around the ad reads, and belongs to no site
    assert lines.count("report built") == 2
    assert any(line.startswith("[autogidas] args --site autogidas --backfill-descriptions")
               and line.endswith("--description-limit 7") for line in lines)
    steps = [line for line in lines if line.startswith("▶")]
    assert [s[:13] for s in steps] == [f"▶ step {n} of 4" for n in (1, 2, 3, 4)]
    # one site failing doesn't stop the run, but it is reported
    assert code == 1 and lines[-1] == "Update had failures: scrape autogidas."


def test_update_force_and_nightly_reach_the_scraper(tmp_path, monkeypatch, capsys):
    _, lines = run(tmp_path, monkeypatch, capsys, "--force", "--nightly", "--reads", "0")
    limit = update.SETTINGS["autoplius"]["nightly_limit"]
    assert f"[autoplius] args --site autoplius --force --limit {limit}" in lines
    assert lines.count("report built") == 1                 # no reads: one rebuild
    assert not any("--backfill-descriptions" in line for line in lines)


def test_only_one_update_at_a_time(tmp_path, monkeypatch, capsys):
    held = update.only_one(tmp_path / "update.lock")
    assert held is not None
    assert update.only_one(tmp_path / "update.lock") is None        # the nightly one, say
    # main() says so and leaves without scraping anything
    code, lines = run(tmp_path, monkeypatch, capsys)
    assert code == 0 and len(lines) == 1 and "already running" in lines[0]
    held.close()
    assert update.only_one(tmp_path / "update.lock") is not None    # free again
