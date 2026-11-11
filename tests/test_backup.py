"""Database backups: a good copy, old ones pruned, failures kept for the dashboard."""

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

import backup
from collectors.store import Store


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "live.db"
    store = Store(path)
    store.site("autoplius").upsert_many(
        [{"id": str(i), "title": "BMW 320", "year": 2015, "price_eur": 9000 + i,
          "mileage_km": 200_000, "fuel": "Dyzelinas", "engine_l": "2.0",
          "link": f"https://x/{i}"} for i in range(25)], "bmw", "320")
    yield path, store                       # left open: backups run while it is in use
    store.close()


def test_backup_is_a_complete_copy_made_while_the_database_is_open(db, tmp_path):
    path, _ = db
    extra = tmp_path / "config.local.toml"
    extra.write_text("[web]\n", encoding="utf-8")
    dest = tmp_path / "drive" / "backups"                       # created on demand
    copy, listings = backup.run(path, dest, keep=5, extras=[extra, tmp_path / "missing.toml"])
    assert listings == 25 and copy.parent == dest and copy.name.startswith("car-scraper-")
    con = sqlite3.connect(copy)
    assert con.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 25
    assert con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 25
    con.close()
    assert sorted(p.name for p in dest.iterdir()) == [copy.name, "config.local.toml"]


def test_only_the_newest_copies_are_kept(db, tmp_path):
    path, _ = db
    dest = tmp_path / "backups"
    dest.mkdir()
    for day in range(1, 6):                                     # five older backups
        (dest / f"car-scraper-2026-11-0{day}_030000.db").write_bytes(b"old")
    (dest / "holiday.jpg").write_bytes(b"not ours")
    copy, _ = backup.run(path, dest, keep=3)
    names = sorted(p.name for p in dest.iterdir())
    assert names == ["car-scraper-2026-11-04_030000.db", "car-scraper-2026-11-05_030000.db",
                     copy.name, "holiday.jpg"]


def test_due_every_three_days(db, tmp_path):
    path, _ = db
    dest = tmp_path / "backups"
    assert backup.is_due(dest, 3)                               # never backed up
    copy, _ = backup.run(path, dest)
    assert not backup.is_due(dest, 3)
    now = datetime.now(timezone.utc)
    assert not backup.is_due(dest, 3, now + timedelta(days=2))
    assert backup.is_due(dest, 3, now + timedelta(days=3))
    old = time.time() - 4 * 86400
    os.utime(copy, (old, old))
    assert backup.is_due(dest, 3)


def test_scheduled_check_backs_up_once_and_reports(db, tmp_path, monkeypatch, capsys):
    path, _ = db
    monkeypatch.setitem(backup.SETTINGS["paths"], "database", str(path))
    cfg = {"dir": str(tmp_path / "backups"), "every_days": 3.0, "keep": 5}
    assert "Backup written" in backup.run_if_due(cfg)
    assert backup.run_if_due(cfg) is None                       # not due again
    state = backup.status(cfg)
    assert state["count"] == 1 and state["error"] is None and state["last"]
    assert "Backup written" in capsys.readouterr().out
    # switched off
    assert backup.status({"dir": "", "every_days": 3.0, "keep": 5}) is None
    assert backup.run_if_due({"dir": " ", "every_days": 3.0, "keep": 5}) is None


def test_a_failed_backup_is_kept_for_the_dashboard_and_leaves_no_partial_file(
        db, tmp_path, monkeypatch):
    path, _ = db
    monkeypatch.setitem(backup.SETTINGS["paths"], "database", str(path))
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("a file where the backup folder should be", encoding="utf-8")
    cfg = {"dir": str(blocker), "every_days": 3.0, "keep": 5}
    assert backup.run_if_due(cfg).startswith("Backup failed")
    assert backup.status(cfg)["error"]
    # and a good one afterwards clears it
    good = {"dir": str(tmp_path / "backups"), "every_days": 3.0, "keep": 5}
    assert "Backup written" in backup.run_if_due(good)
    assert backup.status(good)["error"] is None
    assert not list((tmp_path / "backups").glob("*.part"))
