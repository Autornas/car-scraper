"""
settings.py — project settings from config.toml, over built-in defaults.

    from settings import SETTINGS, project_path
    delay = SETTINGS["autoplius"]["delay"]
    db = project_path(SETTINGS["paths"]["database"])

A missing file or missing key falls back to DEFAULTS, so a fresh checkout runs
without any config. A key with the wrong type stops with a clear message
instead of failing somewhere deep inside a scrape.
"""

import sys
import tomllib
from copy import deepcopy
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.toml"

DEFAULTS = {
    "paths": {
        "data_dir": "data",
        "reports_dir": "reports",
        "database": "data/autoplius.db",
    },
    "autoplius": {
        "enabled": True,
        "delay": 2.0,
        "concurrency": 1,
        "max_age_days": 7.0,
        "empty_max_age_days": 30.0,
        "nightly_limit": 500,
        "description_delay": 2.0,
        "block_threshold": 8,
    },
    "autogidas": {
        "enabled": False,
        "delay": 2.0,
        "concurrency": 1,
        "max_age_days": 7.0,
        "empty_max_age_days": 30.0,
        "nightly_limit": 100,
        "description_delay": 2.0,
        "block_threshold": 8,
    },
    "watcher": {
        "interval_minutes": 3.0,
        "margin_pages": 1,
        "max_pages": 50,
        "delay": 1.0,
        "read_cheap_ads": True,
        "min_similar": 5,
        "min_discount": 0.10,
    },
    "report": {
        "bucket": 3,
        "min_n": 5,
        "candidates": 300,
        "nightly_reads": 150,
        "stale_days": 10,
        "pooled": True,
    },
    "backup": {
        "dir": "",
        "every_days": 3.0,
        "keep": 5,
    },
    "alerts": {
        "deals": True,
        "watcher_failures": True,
    },
    "web": {
        "host": "127.0.0.1",
        "port": 8765,
        "auth": "local",
        "team_domain": "",
        "audience": "",
        "admins": [],
        "viewers": [],
        "viewer_descriptions": True,
    },
    "dashboard": {
        "start_watcher": True,
    },
}


def _merge(defaults, overrides, where):
    merged = deepcopy(defaults)
    for key, value in overrides.items():
        name = f"{where}.{key}" if where else key
        if key not in defaults:
            print(f"config.toml: ignoring unknown setting '{name}'", file=sys.stderr)
            continue
        default = defaults[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                sys.exit(f"config.toml: [{name}] must be a section")
            merged[key] = _merge(default, value, name)
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                sys.exit(f"config.toml: '{name}' must be true or false, got {value!r}")
            merged[key] = value
        elif isinstance(default, (int, float)):
            # Whole numbers are fine where a float is expected (delay = 1).
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or (isinstance(default, int) and not isinstance(default, bool)
                        and not isinstance(value, int)):
                sys.exit(f"config.toml: '{name}' must be a number like {default!r}, "
                         f"got {value!r}")
            merged[key] = type(default)(value)
        else:
            if not isinstance(value, type(default)):
                sys.exit(f"config.toml: '{name}' must be text, got {value!r}")
            merged[key] = value
    return merged


def _deep_update(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def load(path=CONFIG_PATH, local_path=None):
    """config.toml, then config.local.toml (same folder, not in git) on top:
    settings that belong to one PC, like [web] auth and admins, go there."""
    path = Path(path)
    local_path = Path(local_path) if local_path else path.with_name("config.local.toml")
    data = {}
    for p in (path, local_path):
        if not p.exists():
            continue
        try:
            _deep_update(data, tomllib.loads(p.read_text(encoding="utf-8")))
        except tomllib.TOMLDecodeError as exc:
            sys.exit(f"{p} is not valid TOML: {exc}")
    return _merge(DEFAULTS, data, "") if data else deepcopy(DEFAULTS)


def project_path(value):
    """A path from config.toml, resolved against the project folder."""
    p = Path(value)
    return p if p.is_absolute() else PROJECT_ROOT / p


SETTINGS = load()
