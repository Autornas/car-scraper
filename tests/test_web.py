"""The website: sign-in, roles, and what a viewer can never do."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import autoplius_stats as st
import scraper_dashboard as launcher
from collectors import alerts
from collectors.store import Store
from web import admin
from web.app import create_app
from web.auth import Authenticator

TEAM = "team.cloudflareaccess.com"
AUD = "aud-tag-123"
ADMIN, VIEWER, STRANGER = "boss@example.com", "friend@example.com", "nobody@example.com"
SITE = "https://deals.example.com"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNING_KEY, OTHER_KEY = rsa_key(), rsa_key()


class Keys:
    """Stands in for Cloudflare's published keys."""

    def get(self, kid):
        return SIGNING_KEY.public_key() if kid == "k1" else None


def token(email=ADMIN, key=SIGNING_KEY, kid="k1", aud=AUD, iss=f"https://{TEAM}", ttl=600):
    now = int(time.time())
    return jwt.encode({"email": email, "aud": [aud], "iss": iss, "iat": now - 5,
                       "exp": now + ttl, "sub": "x"}, key, algorithm="RS256",
                      headers={"kid": kid})


class FakeManager:
    """A task runner that runs nothing, to see what the app asks of it."""

    def __init__(self):
        self.calls = []

    def start(self, action):
        self.calls.append(("start", action))
        return True, f"Started {action}."

    def stop(self):
        self.calls.append(("stop",))
        return True, "Stopped."

    def snapshot(self):
        return None, []


WEB = {"host": "127.0.0.1", "port": 8765, "auth": "cloudflare", "team_domain": TEAM,
       "audience": AUD, "admins": [ADMIN], "viewers": [VIEWER], "viewer_descriptions": True}


@pytest.fixture
def site(tmp_path, monkeypatch):
    """The app in cloudflare mode, over a scratch database and report files."""
    db = tmp_path / "t.db"
    Store(db).close()
    full, public = tmp_path / "report.html", tmp_path / "report_public.html"
    full.write_text("<html><body>FULL report with labels</body></html>", encoding="utf-8")
    public.write_text("<html><body>viewer report</body></html>", encoding="utf-8")
    monkeypatch.setattr(admin, "DB_PATH", db)
    monkeypatch.setattr(admin, "REPORT_PATH", full)
    monkeypatch.setattr(admin, "PUBLIC_REPORT_PATH", public)
    managers = {"jobs": FakeManager(), "watcher": FakeManager()}
    app = create_app({"web": WEB}, Authenticator(WEB, keys=Keys()), managers)
    client = TestClient(app, base_url=SITE)
    return client, managers


def as_user(email, **kw):
    return {"Cf-Access-Jwt-Assertion": token(email, **kw)}


def post(client, payload, email=ADMIN, origin=SITE, content_type="application/json"):
    headers = {**as_user(email), "Content-Type": content_type}
    if origin:
        headers["Origin"] = origin
    import json
    return client.post("/api/action", content=json.dumps(payload), headers=headers)


# --------------------------------------------------------------------------- #
# sign-in
# --------------------------------------------------------------------------- #
def test_no_token_no_entry(site):
    client, _ = site
    for path in ("/", "/report", "/admin", "/api/me", "/api/status"):
        assert client.get(path).status_code == 401


@pytest.mark.parametrize("bad", [
    dict(key=OTHER_KEY),                       # forged: signed with someone else's key
    dict(kid="k9"),                            # a key Cloudflare never published
    dict(aud="another-app"),                   # a token for a different application
    dict(iss="https://evil.cloudflareaccess.com"),
    dict(ttl=-60),                             # expired
])
def test_bad_tokens_are_refused(site, bad):
    client, _ = site
    assert client.get("/api/me", headers=as_user(ADMIN, **bad)).status_code == 401


def test_a_plain_email_header_is_not_a_login(site):
    client, _ = site
    r = client.get("/api/me", headers={"Cf-Access-Authenticated-User-Email": ADMIN})
    assert r.status_code == 401


def test_valid_login_but_not_on_the_list(site):
    client, _ = site
    assert client.get("/api/me", headers=as_user(STRANGER)).status_code == 403


def test_roles(site):
    client, _ = site
    assert client.get("/api/me", headers=as_user(ADMIN)).json() == {"email": ADMIN, "role": "admin"}
    assert client.get("/api/me", headers=as_user(VIEWER.upper())).json()["role"] == "viewer"


def test_the_cloudflare_cookie_works_too(site):
    client, _ = site
    client.cookies.set("CF_Authorization", token(VIEWER))
    assert client.get("/api/me").json()["role"] == "viewer"


# --------------------------------------------------------------------------- #
# what a viewer gets, and can never do
# --------------------------------------------------------------------------- #
def test_viewer_gets_the_viewer_report_only(site):
    client, _ = site
    for path in ("/", "/report"):
        r = client.get(path, headers=as_user(VIEWER))
        assert r.status_code == 200 and "viewer report" in r.text and "FULL" not in r.text


def test_admin_gets_the_full_report(site):
    client, _ = site
    assert "FULL report" in client.get("/report", headers=as_user(ADMIN)).text
    home = client.get("/", headers=as_user(ADMIN)).text
    assert "viewer report" in home and 'href="/admin"' in home


@pytest.mark.parametrize("path", ["/admin", "/api/status", "/api/models"])
def test_viewer_cannot_open_admin_pages(site, path):
    client, _ = site
    assert client.get(path, headers=as_user(VIEWER)).status_code == 403


@pytest.mark.parametrize("payload", [
    {"action": "start_database"}, {"action": "rescrape_all"}, {"action": "stop"},
    {"action": "start_watcher"}, {"action": "stop_watcher"},
    {"action": "label", "source": "autoplius", "id": "1", "label": "deal"},
    {"action": "save_database_models", "text": "bmw/320\n"},
])
def test_viewer_cannot_change_anything_even_calling_the_api(site, payload):
    client, managers = site
    r = post(client, payload, email=VIEWER)
    assert r.status_code == 403
    assert managers["jobs"].calls == [] and managers["watcher"].calls == []


# --------------------------------------------------------------------------- #
# admin actions and their guards
# --------------------------------------------------------------------------- #
def test_admin_starts_tasks_and_the_watcher_separately(site):
    client, managers = site
    assert post(client, {"action": "start_database"}).status_code == 202
    assert post(client, {"action": "start_watcher"}).status_code == 202
    assert post(client, {"action": "stop_watcher"}).status_code == 200
    assert managers["jobs"].calls == [("start", "start_database")]
    assert managers["watcher"].calls == [("start", "start_watcher"), ("stop",)]
    actions = [a["action"] for a in admin.recent_audit()]
    assert actions == ["stop", "start", "start"]
    assert all(a["email"] == ADMIN for a in admin.recent_audit())


def test_admin_labels_a_car(site):
    client, _ = site
    r = post(client, {"action": "label", "source": "autoplius", "id": "32908972",
                      "label": "fake"})
    assert r.status_code == 200 and r.json()["message"] == "Marked as fake price."


@pytest.mark.parametrize("origin", [None, "https://evil.example.com", "http://deals.example.com.evil.io"])
def test_changes_must_come_from_this_sites_pages(site, origin):
    client, managers = site
    assert post(client, {"action": "start_database"}, origin=origin).status_code == 403
    assert managers["jobs"].calls == []


def test_changes_must_be_json(site):
    client, _ = site
    r = post(client, {"action": "start_database"}, content_type="text/plain")
    assert r.status_code == 415


def test_unknown_actions_and_bad_input(site):
    client, _ = site
    assert post(client, {"action": "rm -rf"}).status_code == 400
    assert post(client, {"action": "label", "source": "x", "id": "1", "label": "deal"}).status_code == 400
    assert post(client, {"action": "save_database_models", "text": 5}).status_code == 400


def test_security_headers_on_every_answer(site):
    client, _ = site
    for r in (client.get("/api/me"), client.get("/", headers=as_user(VIEWER))):
        assert "frame-ancestors 'self'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"


def test_report_is_cached_by_etag(site):
    client, _ = site
    first = client.get("/", headers=as_user(VIEWER))
    again = client.get("/", headers={**as_user(VIEWER), "If-None-Match": first.headers["etag"]})
    assert again.status_code == 304 and again.content == b""


# --------------------------------------------------------------------------- #
# local mode
# --------------------------------------------------------------------------- #
LOCAL = {**WEB, "auth": "local", "admins": [], "viewers": []}


def test_local_mode_is_this_pc_only(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "DB_PATH", tmp_path / "t.db")
    app = create_app({"web": LOCAL}, Authenticator(LOCAL),
                     {"jobs": FakeManager(), "watcher": FakeManager()})
    here = TestClient(app, base_url="http://127.0.0.1:8765")
    assert here.get("/api/me").json() == {"email": "local", "role": "admin"}
    assert TestClient(app, base_url="http://localhost:8765").get("/api/me").status_code == 200
    # what a request through a tunnel would look like
    assert TestClient(app, base_url=SITE).get("/api/me").status_code == 403


def test_cloudflare_mode_needs_its_settings():
    for missing in ({"team_domain": ""}, {"audience": ""}, {"admins": []}):
        with pytest.raises(ValueError):
            Authenticator({**WEB, **missing})
    with pytest.raises(ValueError):
        Authenticator({**WEB, "auth": "open"})


def test_tailscale_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "DB_PATH", tmp_path / "t.db")
    cfg = {**WEB, "auth": "tailscale", "admins": [ADMIN], "viewers": [VIEWER]}
    app = create_app({"web": cfg}, Authenticator(cfg),
                     {"jobs": FakeManager(), "watcher": FakeManager()})
    ts = "https://pc.tail1234.ts.net"
    client = TestClient(app, base_url=ts)
    login = lambda who: {"Tailscale-User-Login": who}
    assert client.get("/api/me", headers=login(ADMIN)).json()["role"] == "admin"
    assert client.get("/api/me", headers=login(VIEWER)).json()["role"] == "viewer"
    assert client.get("/api/me", headers=login("stranger@x.com")).status_code == 403
    assert client.get("/api/me").status_code == 403                       # funnel: no identity
    assert client.get("/admin", headers=login(VIEWER)).status_code == 403
    # a forged header through any other hostname is useless
    assert TestClient(app, base_url=SITE).get("/api/me", headers=login(ADMIN)).status_code == 403
    assert TestClient(app, base_url="http://127.0.0.1:8765").get(
        "/api/me", headers=login(ADMIN)).status_code == 403
    with pytest.raises(ValueError):
        Authenticator({**cfg, "admins": []})


def test_launcher_only_listens_on_this_pc():
    assert launcher.is_loopback("127.0.0.1") and launcher.is_loopback("localhost")
    assert launcher.is_loopback("::1")
    assert not launcher.is_loopback("0.0.0.0") and not launcher.is_loopback("192.168.1.5")


# --------------------------------------------------------------------------- #
# viewer copy of the report, alerts
# --------------------------------------------------------------------------- #
def test_viewer_copy_holds_no_labels():
    payload = {"cols": ["make", "description", "description_checked", "label"],
               "cars": [["bmw", "Daužtas priekis", 1, "deal"]],
               "meta": {"verdicts": [{"verdict": "deal", "n": 1, "labelled": 1, "agreed": 1}]}}
    with_text = st.viewer_payload(payload, descriptions=True)
    assert with_text["cars"] == [["bmw", "Daužtas priekis", 1, ""]]
    assert with_text["meta"]["verdicts"][0]["labelled"] == 0 and with_text["meta"]["viewer"]
    without = st.viewer_payload(payload, descriptions=False)
    assert without["cars"] == [["bmw", "", 0, ""]]
    assert payload["cars"] == [["bmw", "Daužtas priekis", 1, "deal"]]     # original untouched


def test_alerts_are_off_without_a_token(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert alerts.telegram_config(tmp_path / "none.toml") is None
    assert alerts.send("hi", config=None, post=lambda *a, **k: 1 / 0) is False


def test_alerts_read_the_secrets_file_and_send(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    secrets = tmp_path / "secrets.toml"
    secrets.write_text('[telegram]\nbot_token = "123:ABC"\nchat_id = "42"\n', encoding="utf-8")
    config = alerts.telegram_config(secrets)
    sent = []

    class OK:
        status_code = 200
    assert alerts.send("Deal!", config, post=lambda url, **kw: sent.append((url, kw)) or OK())
    assert sent[0][0] == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert sent[0][1]["json"]["chat_id"] == "42"


def test_deal_message():
    text = alerts.deal_message("autogidas", "BMW 320", 2015, 7000, 10_000, 8_000, "https://x/1",
                               {"source": "autoplius", "price_eur": 7200, "link": "https://y/2"})
    assert "Deal on autogidas: BMW 320 2015 for €7,000" in text
    assert "12% under the low end" in text and "Also on autoplius for €7,200" in text
