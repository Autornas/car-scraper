"""
Site collectors. Each site module knows one site's URLs and pages; the shared
modules do the rest:

    collectors.common   fetching, refusal detection, name helpers
    collectors.store    the database (every row tagged with its source site)
    collectors.crawl    full scrape, damage sweep, ad pages, coverage
    collectors.watch    the gap-free new-listing watcher
"""

from collectors import autoplius

SITES = {autoplius.SOURCE: autoplius}


def enabled_sites():
    """Site modules switched on in config.toml ([autoplius] enabled = true ...)."""
    from settings import SETTINGS
    return [module for name, module in SITES.items()
            if SETTINGS.get(name, {}).get("enabled", False)]


def get_site(name):
    try:
        return SITES[name]
    except KeyError:
        raise SystemExit(f"Unknown site '{name}'. Known: {', '.join(SITES)}") from None
