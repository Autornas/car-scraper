"""config.toml loading (scripts/settings.py)."""

import pytest

import settings


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_file_gives_defaults(tmp_path):
    assert settings.load(tmp_path / "nope.toml") == settings.DEFAULTS


def test_values_override_defaults(tmp_path):
    cfg = settings.load(write(tmp_path, "[watcher]\ninterval_minutes = 2\nmax_pages = 10\n"))
    assert cfg["watcher"]["interval_minutes"] == 2.0
    assert isinstance(cfg["watcher"]["interval_minutes"], float)
    assert cfg["watcher"]["max_pages"] == 10
    assert cfg["watcher"]["delay"] == settings.DEFAULTS["watcher"]["delay"]


def test_local_file_overrides_config(tmp_path):
    path = write(tmp_path, '[web]\nauth = "local"\nport = 9000\n[watcher]\nmax_pages = 10\n')
    (tmp_path / "config.local.toml").write_text(
        '[web]\nauth = "tailscale"\nadmins = ["me@x.com"]\n', encoding="utf-8")
    cfg = settings.load(path)
    assert cfg["web"]["auth"] == "tailscale" and cfg["web"]["admins"] == ["me@x.com"]
    assert cfg["web"]["port"] == 9000 and cfg["watcher"]["max_pages"] == 10


def test_unknown_keys_are_ignored(tmp_path, capsys):
    cfg = settings.load(write(tmp_path, "[watcher]\ntypo_key = 1\n"))
    assert "typo_key" not in cfg["watcher"]
    assert "unknown setting 'watcher.typo_key'" in capsys.readouterr().err


@pytest.mark.parametrize("text", [
    '[watcher]\nmax_pages = "three"\n',
    "[watcher]\nmax_pages = 2.5\n",
    '[report]\npooled = "yes"\n',
    "watcher = 5\n",
    "[paths]\ndatabase = 5\n",
])
def test_wrong_types_stop_with_a_message(tmp_path, text):
    with pytest.raises(SystemExit) as exc:
        settings.load(write(tmp_path, text))
    assert "config.toml" in str(exc.value)


def test_invalid_toml_stops(tmp_path):
    with pytest.raises(SystemExit):
        settings.load(write(tmp_path, "[watcher\n"))


def test_project_config_loads():
    cfg = settings.load()
    assert cfg["paths"]["database"].endswith("autoplius.db")


def test_relative_paths_resolve_to_project():
    assert settings.project_path("data") == settings.PROJECT_ROOT / "data"
