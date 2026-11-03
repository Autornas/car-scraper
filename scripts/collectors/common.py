"""
collectors/common.py — what every site collector shares: polite fetching with
refusal detection, name helpers, and Ctrl+C handling.
"""

import re
import signal
import sys
import threading
import time
from pathlib import Path

import requests

from settings import SETTINGS, project_path

DATA_DIR = project_path(SETTINGS["paths"]["data_dir"])

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8",
}


class BlockDetector:
    """
    Notices when the site is refusing us (403 / connection resets) and trips
    once it's clearly not going to serve us — so we stop grinding through
    2000 pages against a closed door. Also saves the first refused response
    so you can SEE what kind of refusal it is.
    """

    def __init__(self, threshold=8, dump_path=DATA_DIR / "blocked_response.html"):
        self.threshold = threshold
        self.dump_path = Path(dump_path)
        self.first_status = None
        self.tripped = threading.Event()
        self._consecutive = 0
        self._dumped = False
        self._lock = threading.Lock()

    def record_refusal(self, status=None, body=None):
        with self._lock:
            self._consecutive += 1
            if self.first_status is None and status is not None:
                self.first_status = status
            if body and not self._dumped:
                try:
                    self.dump_path.parent.mkdir(parents=True, exist_ok=True)
                    self.dump_path.write_text(body, encoding="utf-8", errors="replace")
                    self._dumped = True
                    print(f"  ! saved the refused page to {self.dump_path.name} — "
                          f"open it in a browser to see what the site actually sent",
                          file=sys.stderr)
                except OSError:
                    pass
            if self._consecutive >= self.threshold:
                self.tripped.set()

    def record_success(self):
        with self._lock:
            self._consecutive = 0


def diagnose(body: str) -> str:
    """Guess the kind of refusal from the page the server sent back."""
    low = (body or "").lower()
    if any(k in low for k in ("recaptcha", "hcaptcha", "turnstile", "g-recaptcha")):
        return "a real CAPTCHA widget — the site wants a human to solve something"
    if any(k in low for k in ("checking your browser", "cf-browser-verification",
                              "just a moment", "enable javascript and cookies")):
        return "a Cloudflare browser challenge (JavaScript check)"
    if "cloudflare" in low and ("access denied" in low or "error 1006" in low
                               or "error 1007" in low or "error 1008" in low):
        return "Cloudflare blocking this IP outright (no challenge offered)"
    if any(k in low for k in ("access denied", "forbidden", "prieiga uždrausta",
                              "blocked", "ip address has been")):
        return "a flat 'access denied' — this path is refused to this client"
    if not low.strip():
        return "an empty body — that's a firewall/IP-level drop, not a CAPTCHA"
    return "an unrecognised refusal page — open the saved file to see it"


def probe(session, url, timeout=20):
    """Single request, returns (status_code_or_None, body_or_error_text)."""
    try:
        r = session.get(url, headers=HEADERS, timeout=timeout)
        return r.status_code, r.text[:4000]
    except requests.RequestException as exc:
        return None, f"{exc.__class__.__name__}: {exc}"

# How long to wait after "too many requests" (429) or "unavailable" (503)
# before asking again, unless the site says how long in Retry-After. A site
# that is still refusing after the last wait gets left alone for this run.
BACKOFF_SECONDS = (30, 90, 240)
# What both sites answer for an ad that has been taken down (sold, withdrawn).
AD_GONE_STATUS = (404, 410)
MAX_RETRY_AFTER = 600


def _retry_after(resp):
    try:
        return max(0, int(resp.headers.get("Retry-After", "")))
    except ValueError:
        return None


def _pause(seconds):
    """Sleep, but wake up for Ctrl+C / Stop."""
    end = time.monotonic() + seconds
    while time.monotonic() < end and not STOP.is_set():
        time.sleep(min(1.0, end - time.monotonic()))


def fetch(session: requests.Session, url: str, retries: int = 3, blocks=None) -> str:
    """GET a page. Transient errors are retried a few times; a rate limit
    (429 / 503) is waited out with growing pauses; other refusals (401 / 403)
    are recorded and given up on at once, since asking again from the same
    IP seconds later only adds load."""
    backoff = 0
    attempt = 0
    while attempt < retries:
        attempt += 1
        if blocks is not None and blocks.tripped.is_set():
            return ""
        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 200:
                resp.encoding = resp.apparent_encoding or "utf-8"
                if blocks is not None:
                    blocks.record_success()
                return resp.text
            print(f"  ! HTTP {resp.status_code} for {url}", file=sys.stderr)
            if resp.status_code in (429, 503) and backoff < len(BACKOFF_SECONDS) \
                    and not STOP.is_set():
                wait = _retry_after(resp) or BACKOFF_SECONDS[backoff]
                if wait <= MAX_RETRY_AFTER:
                    backoff += 1
                    attempt -= 1             # a rate-limit wait isn't a failed attempt
                    print(f"    the site asks us to slow down; waiting {wait} s",
                          file=sys.stderr)
                    _pause(wait)
                    continue
            if resp.status_code in (401, 403, 429, 503):
                if blocks is not None:
                    blocks.record_refusal(resp.status_code, resp.text)
                return ""
        except requests.RequestException as exc:
            print(f"  ! {exc.__class__.__name__}: {exc}", file=sys.stderr)
            if blocks is not None:
                blocks.record_refusal()
        if attempt < retries:
            time.sleep(3 * attempt)
    return ""


def clean_number(text: str):
    """'22 750' / '22.750' -> 22750"""
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def normalized_label(value: str) -> str:
    """Normalize make/model labels for matching listing headings."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def name_slug(text):
    """"Mercedes-Benz" -> "mercedes-benz", "A klasė" -> "a-klasė": the make/model
    keys the database uses, built from the site's display names. "+" is kept
    as "plus", or "Prius+" and "Prius" would share a key."""
    text = (text or "").lower().replace("+", " plus ")
    return "-".join("".join(ch if ch.isalnum() else " " for ch in text).split())


def resolve_model_from_title(title: str, make: str, models):
    """Return the most-specific known model matching a listing's title, if any."""
    normalized_title = normalized_label(title)
    normalized_make = normalized_label(make)
    if not normalized_title.startswith(normalized_make):
        return None

    model_part = normalized_title[len(normalized_make):]
    matches = [
        candidate for candidate in models
        if model_part.startswith(normalized_label(candidate))
    ]
    return max(matches, key=lambda candidate: len(normalized_label(candidate))) \
        if matches else None


def title_fits(title, make, model, known_models):
    """
    Does a card belong in this make/model's search? Searches go by the site's
    own ids, so results are that model; this only guards against the site
    answering a bad search with unrelated cars (an old slug URL returned all
    46,676). Reject a card whose title names another make, or names another
    known model of this make; accept one that names this model or none that
    we know.
    """
    if not normalized_label(title).startswith(normalized_label(make)):
        return False
    matched = resolve_model_from_title(title, make, known_models)
    return matched is None or matched == model

STOP = threading.Event()


def install_sigint_handler():
    """First Ctrl-C stops politely and keeps the data; second one exits hard."""
    def handler(signum, frame):
        if STOP.is_set():
            raise KeyboardInterrupt
        STOP.set()
        print("\n! Interrupted — finishing the pages in flight. Everything scraped "
              "so far is already saved. Press Ctrl-C again to quit immediately.",
              file=sys.stderr)
    try:
        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):
        pass


def load_ids_file(path):
    with open(path, encoding="utf-8") as fh:
        return [line.split()[0] for line in fh
                if line.strip() and not line.lstrip().startswith("#")]


def load_models_file(path):
    """'make/model' lines -> a set of pairs to restrict a run to."""
    pairs = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "/" in line:
            make, model = line.split("/", 1)
            pairs.add(f"{make.strip().lower()}/{model.strip().lower()}")
    return pairs


AGE_UNITS = {"min": 1, "val": 60, "d": 1440, "sav": 10080, "mėn": 43200}


def parse_age(text):
    """Minutes since publishing, from a card's badge: "Prieš 1 val. 15 min." -> 75,
    "Mažiau nei prieš minutę" -> 0. None for anything else (older ads carry no
    badge at all)."""
    text = (text or "").strip().lower()
    if not text:
        return None
    if "mažiau nei" in text:
        return 0
    parts = re.findall(r"(\d+)\s*(min|val|d|sav|mėn)\b", text)
    return sum(int(n) * AGE_UNITS[unit] for n, unit in parts) if parts else None
