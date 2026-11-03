"""
web/auth.py — who is asking, and what they may do.

Two modes, chosen in config.toml ([web] auth):

  local       Only you, on this PC. Every request is the admin, and only
              requests addressed to localhost / 127.0.0.1 are served: a
              request that arrives through a tunnel carries the public
              hostname and is refused, so this mode can't leak by accident.

  cloudflare  For use behind Cloudflare Tunnel + Access. Cloudflare signs
              every request it lets through with a token in the
              Cf-Access-Jwt-Assertion header (also the CF_Authorization
              cookie). It is verified here, signature included, against
              your team's published keys, the application's audience tag,
              the issuer and the expiry. The email inside decides the role:
              [web] admins / viewers. Anyone else is refused, even with a
              valid Cloudflare login.

Never trust a plain email header: only the signed token counts.
"""

import threading
import time
from dataclasses import dataclass

import jwt
import requests

ROLES = ("admin", "viewer")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


@dataclass(frozen=True)
class User:
    email: str
    role: str

    @property
    def is_admin(self):
        return self.role == "admin"


class AuthError(Exception):
    """Refused. `status` is the HTTP status to answer with."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class CloudflareKeys:
    """Your Cloudflare Access team's signing keys, fetched from
    https://<team>/cdn-cgi/access/certs and cached; fetched again when a token
    names a key we don't have (Cloudflare rotates them)."""

    def __init__(self, team_domain, ttl=3600, fetch=None):
        self.url = f"https://{team_domain}/cdn-cgi/access/certs"
        self.ttl = ttl
        self._fetch = fetch or (lambda url: requests.get(url, timeout=10).json())
        self._keys, self._at = {}, 0.0
        self._lock = threading.Lock()

    def get(self, kid):
        with self._lock:
            if kid not in self._keys or time.monotonic() - self._at > self.ttl:
                data = self._fetch(self.url)
                self._keys = {k["kid"]: jwt.algorithms.RSAAlgorithm.from_jwk(k)
                              for k in data.get("keys", []) if "kid" in k}
                self._at = time.monotonic()
            return self._keys.get(kid)


class Authenticator:
    def __init__(self, cfg, keys=None):
        self.mode = cfg["auth"]
        if self.mode not in ("local", "cloudflare"):
            raise ValueError(f"[web] auth must be 'local' or 'cloudflare', not {self.mode!r}")
        self.admins = {e.strip().lower() for e in cfg["admins"] if isinstance(e, str)}
        self.viewers = {e.strip().lower() for e in cfg["viewers"] if isinstance(e, str)}
        self.team_domain = cfg["team_domain"].strip()
        self.audience = cfg["audience"].strip()
        if self.mode == "cloudflare":
            if not self.team_domain or not self.audience:
                raise ValueError("[web] auth = 'cloudflare' needs team_domain "
                                 "(e.g. yourteam.cloudflareaccess.com) and audience "
                                 "(the Access application's AUD tag)")
            if not self.admins:
                raise ValueError("[web] auth = 'cloudflare' needs at least one email in admins")
        self.keys = keys or (CloudflareKeys(self.team_domain) if self.mode == "cloudflare" else None)

    def role_for(self, email):
        email = (email or "").lower()
        if email in self.admins:
            return "admin"
        if email in self.viewers:
            return "viewer"
        return None

    def identify(self, host, headers, cookies):
        """The User making a request, or AuthError."""
        if self.mode == "local":
            host = (host or "").lower()
            hostname = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
            if hostname not in LOCAL_HOSTS:
                raise AuthError(403, "This dashboard only answers on this PC. To use it from "
                                     "elsewhere, set [web] auth = 'cloudflare'.")
            return User("local", "admin")

        token = headers.get("cf-access-jwt-assertion") or cookies.get("CF_Authorization")
        if not token:
            raise AuthError(401, "Not signed in through Cloudflare Access.")
        email = self.verify(token)
        role = self.role_for(email)
        if role is None:
            raise AuthError(403, f"{email} has no access to this site.")
        return User(email, role)

    def verify(self, token):
        """The email from a valid Cloudflare Access token, or AuthError."""
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            key = self.keys.get(kid)
            if key is None:
                raise AuthError(401, "Login token signed with an unknown key.")
            claims = jwt.decode(token, key=key, algorithms=["RS256"], audience=self.audience,
                                issuer=f"https://{self.team_domain}",
                                options={"require": ["exp", "iat", "aud", "iss"]})
        except AuthError:
            raise
        except (jwt.PyJWTError, requests.RequestException, ValueError) as exc:
            raise AuthError(401, f"Login token rejected: {exc.__class__.__name__}") from None
        email = claims.get("email")
        if not email:
            raise AuthError(401, "Login token has no email (service tokens aren't users).")
        return email
