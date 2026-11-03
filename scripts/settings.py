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
        "delay": 1.0,
        "concurrency": 3,
        "max_age_days": 30.0,
        "description_delay": 2.0,
        "block_threshold": 8,
    },
    "autogidas": {
        "enabled": False,
    },
    "watcher": {
        "interval_minutes": 5.0,
        "margin_pages": 1,
        "max_pages": 50,
        "delay": 1.0,
        "read_cheap_ads": True,
    },
    "report": {
        "bucket": 3,
        "min_n": 5,
        "candidates": 300,
        "pooled": True,
    },
    "dashboard": {
        "port": 8765,
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


def load(path=CONFIG_PATH):
    path = Path(path)
    if not path.exists():
        return deepcopy(DEFAULTS)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        sys.exit(f"{path} is not valid TOML: {exc}")
    return _merge(DEFAULTS, data, "")


def project_path(value):
    """A path from config.toml, resolved against the project folder."""
    p = Path(value)
    return p if p.is_absolute() else PROJECT_ROOT / p


SETTINGS = load()
