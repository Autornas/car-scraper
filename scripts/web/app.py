"""
web/app.py — the website: the deals report for everyone signed in, the
control area for admins.

    /             the report, viewer copy (no labels; seller text only if
                  [web] viewer_descriptions)
    /report       the report: admins get the full copy, viewers the viewer copy
    /admin        the control page (admins)
    /api/me       who you are
    /api/status, /api/models, /api/action     admin only

Every request is identified first (web/auth.py); admin pages and every
admin API refuse viewers on the server, whatever the page shows. Changes
(POST) must be JSON, come from this site's own pages (Origin = Host) and
are written to the audit log.

Run it with:  python scripts/scraper_dashboard.py
"""

import json
import sqlite3
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

from settings import SETTINGS
from web import admin
from web.auth import Authenticator, AuthError, User

MAX_BODY = 200_000

# Inline scripts and styles are how the report and control page are built, so
# 'unsafe-inline' stays; everything else is locked to this site: no outside
# scripts, no framing by other sites, no form posts elsewhere, no plugins.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
       "frame-src 'self'; frame-ancestors 'self'; form-action 'self'; base-uri 'none'; "
       "object-src 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
NO_REPORT = """<!doctype html><meta charset="utf-8"><title>No report yet</title>
<body style="font:15px system-ui;padding:40px;color:#444">The price report hasn't been built
yet. An admin can build it from the control page (Rebuild report).</body>"""


def create_app(settings=SETTINGS, authenticator=None, managers=None, start_watcher=False):
    auth = authenticator or Authenticator(settings["web"])
    managers = managers or {"jobs": admin.JobManager(), "watcher": admin.JobManager()}

    @asynccontextmanager
    async def lifespan(_app):
        if start_watcher:
            print(managers["watcher"].start("start_watcher")[1], flush=True)
        yield
        for manager in managers.values():           # never leave a scraper running
            task, _ = manager.snapshot()
            if task and task["status"] == "running":
                manager.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=2000)
    app.state.managers = managers

    @app.middleware("http")
    async def identify_and_harden(request: Request, call_next):
        try:
            request.state.user = auth.identify(request.headers.get("host"),
                                               request.headers, request.cookies)
        except AuthError as exc:
            response = JSONResponse({"ok": False, "message": str(exc)}, status_code=exc.status)
        else:
            response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    def user(request: Request) -> User:
        return request.state.user

    def admin_only(current: User = Depends(user)) -> User:
        if not current.is_admin:
            raise HTTPException(403, "Admins only.")
        return current

    # ------------------------------------------------------------ pages
    def report_response(request, path, current):
        try:
            stat = path.stat()
        except OSError:
            return HTMLResponse(NO_REPORT, status_code=404)
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        body = path.read_bytes()
        if current.is_admin and path == admin.PUBLIC_REPORT_PATH:
            body = body.replace(b"</body>", b'<a href="/admin" style="position:fixed;right:14px;'
                                b'bottom:12px;font:13px system-ui">Admin</a></body>', 1)
        return Response(body, media_type="text/html; charset=utf-8",
                        headers={"ETag": etag, "Cache-Control": "private, no-cache"})

    @app.get("/")
    def home(request: Request, current: User = Depends(user)):
        return report_response(request, admin.PUBLIC_REPORT_PATH, current)

    @app.get("/report")
    def report(request: Request, current: User = Depends(user)):
        path = admin.REPORT_PATH if current.is_admin else admin.PUBLIC_REPORT_PATH
        return report_response(request, path, current)

    @app.get("/admin")
    def control_page(_: User = Depends(admin_only)):
        return HTMLResponse(admin.PAGE, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ API
    @app.get("/api/me")
    def me(current: User = Depends(user)):
        return {"email": current.email, "role": current.role}

    @app.get("/api/status")
    def status(current: User = Depends(admin_only)):
        task, logs = managers["jobs"].snapshot()
        watcher_task, watcher_logs = managers["watcher"].snapshot()
        return JSONResponse({
            "task": task, "logs": logs[-150:],
            "watcher_task": watcher_task, "watcher_logs": watcher_logs[-150:],
            "database": admin.database_summary(),
            "report_exists": admin.REPORT_PATH.exists(),
            "audit": admin.recent_audit(),
            "user": {"email": current.email, "role": current.role},
        }, headers={"Cache-Control": "no-store"})

    @app.get("/api/models")
    def models(_: User = Depends(admin_only)):
        if not admin.DB_PATH.exists():
            return {"text": "", "count": 0}
        connection = sqlite3.connect(f"file:{admin.DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            rows = connection.execute(
                """SELECT DISTINCT make, model FROM models
                   WHERE source = 'autoplius' AND enabled = 1 ORDER BY make, model""").fetchall()
        finally:
            connection.close()
        return {"text": "\n".join(f"{mk}/{md}" for mk, md in rows), "count": len(rows)}

    @app.post("/api/action")
    async def action(request: Request, current: User = Depends(admin_only)):
        payload = await read_json(request)
        name = payload.get("action")
        if name == "save_database_models":
            if not isinstance(payload.get("text"), str):
                raise HTTPException(400, "Model list must be text.")
            try:
                text, count, message = admin.save_database_models(payload["text"])
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            admin.audit(current.email, "model list", message)
            return {"ok": True, "message": message, "text": text, "count": count}
        if name == "label":
            try:
                message = admin.save_label(payload)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            admin.audit(current.email, "label",
                        f"{payload['source']}:{payload['id']} {payload['label'] or 'cleared'}")
            return {"ok": True, "message": message}
        if name in ("stop", "stop_watcher"):
            ok, message = managers["watcher" if name == "stop_watcher" else "jobs"].stop()
            if ok:
                admin.audit(current.email, "stop", message)
            return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 409)
        if name not in admin.TASKS:
            raise HTTPException(400, "Unknown action.")
        manager = managers["watcher" if name == "start_watcher" else "jobs"]
        try:
            ok, message = manager.start(name)
        except OSError as exc:
            raise HTTPException(500, f"Could not start: {exc}") from None
        if ok:
            admin.audit(current.email, "start", admin.TASKS[name][0])
        return JSONResponse({"ok": ok, "message": message}, status_code=202 if ok else 409)

    async def read_json(request: Request):
        """A change request must come from this site's own pages, as JSON."""
        origin = request.headers.get("origin")
        if not origin or urlparse(origin).netloc.lower() != (request.headers.get("host") or "").lower():
            raise HTTPException(403, "Changes must come from this site's own pages.")
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "Expected JSON.")
        body = await request.body()
        if not 1 <= len(body) <= MAX_BODY:
            raise HTTPException(400, "Request body is empty or too large.")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(400, "Invalid JSON.") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "Expected a JSON object.")
        return payload

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        return JSONResponse({"ok": False, "message": exc.detail}, status_code=exc.status_code)

    return app
