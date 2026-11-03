"""The dashboard's model-list editor: switches Autoplius models on and off,
never deletes, never touches another site's rows."""

import pytest

import scraper_dashboard as dash
from collectors import autogidas as ag
from collectors import autoplius as ap
from collectors.store import Store


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    store = Store(path)
    store.site("autoplius").sync_catalogue(
        [{"make": "bmw", "model": m, "make_id": 97, "model_id": i, "make_name": "BMW",
          "model_name": m} for i, m in enumerate(["320", "520", "x5"], start=1)],
        ap.catalogue_url)
    store.site("autogidas").sync_catalogue(
        [{"make": "bmw", "model": "*", "make_id": None, "model_id": None,
          "make_name": "BMW", "model_name": "*"}], ag.catalogue_url)
    store.close()
    monkeypatch.setattr(dash, "DB_PATH", path)
    return path


def enabled(path):
    store = Store(path)
    rows = store.conn.execute(
        "SELECT source, make, model, enabled FROM models ORDER BY source, model").fetchall()
    store.close()
    return rows


def test_editor_switches_models_off_without_deleting(db_path):
    text, count, message = dash.save_database_models("bmw/320\nbmw/x5\n")
    assert count == 2 and "1 switched off" in message
    assert enabled(db_path) == [
        ("autogidas", "bmw", "*", 1),                  # another site: untouched
        ("autoplius", "bmw", "320", 1),
        ("autoplius", "bmw", "520", 0),                # kept, just off
        ("autoplius", "bmw", "x5", 1)]


def test_editor_rejects_names_outside_the_catalogue(db_path):
    with pytest.raises(ValueError, match="Not in the Autoplius catalogue: bmw/m9"):
        dash.save_database_models("bmw/320\nbmw/m9\n")


def test_editor_needs_at_least_one_model(db_path):
    with pytest.raises(ValueError):
        dash.save_database_models("# nothing\n\n")
