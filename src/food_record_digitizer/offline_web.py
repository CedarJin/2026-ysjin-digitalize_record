"""Loopback-only, capability-protected offline review UI; no remote authentication."""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .imaging import render_pages
from .ingest import InputError
from .offline_archive import sha256
from .review import ReviewError

STATIC = Path(__file__).parent / "static"


def create_offline_app(reviewer=None, launch_token=None, workspace=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])
    token = launch_token or secrets.token_urlsafe(32)
    session = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    cookie_name = "offline_session_" + sha256(session.encode())[:12]
    app.state.launch_token = token

    def require_package():
        if reviewer is None:
            raise ReviewError("Open a review package first.", 409)
        return reviewer

    @app.exception_handler(ReviewError)
    @app.exception_handler(InputError)
    async def error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=getattr(exc, "status", 400))

    @app.exception_handler(ValidationError)
    async def invalid(request, exc):
        return JSONResponse({"error": "Review content does not match the schema."}, status_code=400)

    @app.middleware("http")
    async def secure(request, call_next):
        if request.url.path.startswith("/api/"):
            if not hmac.compare_digest(request.cookies.get(cookie_name, ""), session):
                return JSONResponse({"error": "Open the review using the local launcher."}, status_code=401)
            if request.method not in {"GET", "HEAD"}:
                origin = request.headers.get("origin")
                if (
                    origin and urlparse(origin).netloc != request.headers.get("host")
                ) or not hmac.compare_digest(request.headers.get("x-csrf-token", ""), csrf):
                    return JSONResponse({"error": "Local session verification failed."}, status_code=403)
                try:
                    limit = (
                        1024 * 1024 * 1024 if request.url.path == "/api/offline/open" else 16 * 1024 * 1024
                    )
                    if not 0 <= int(request.headers.get("content-length", "-1")) <= limit:
                        raise ValueError()
                except ValueError:
                    return JSONResponse({"error": "Request exceeds the local save limit."}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
        )
        return response

    @app.get("/")
    def index(request: Request):
        supplied = request.query_params.get("launch", "")
        if supplied and hmac.compare_digest(supplied, token):
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(cookie_name, session, httponly=True, samesite="strict", path="/")
            return response
        return FileResponse(STATIC / "index.html")

    @app.get("/static/{name}")
    def asset(name: str):
        if name not in {"app.js", "style.css"}:
            raise ReviewError("Not found.", 404)
        return FileResponse(STATIC / name)

    @app.get("/api/me")
    def me():
        if reviewer is None:
            return {
                "id": "local",
                "name": "Reviewer",
                "role": "reviewer",
                "project_id": "",
                "offline": True,
                "package_id": None,
                "csrf": csrf,
            }
        return {
            **reviewer.manifest["reviewer"],
            "role": "reviewer",
            "project_id": reviewer.manifest["project_id"],
            "offline": True,
            "package_id": reviewer.manifest["package_id"],
            "csrf": csrf,
        }

    @app.post("/api/offline/open")
    async def open_package(request: Request):
        nonlocal reviewer
        from .offline_reviewer import OfflineReviewer

        if reviewer is not None:
            raise ReviewError(
                "This window already has a package. Launch another window to open a different package.", 409
            )
        form = await request.form()
        upload = form.get("file")
        if not upload or not hasattr(upload, "read"):
            raise ReviewError("Choose a review package.")
        data = await upload.read(1024 * 1024 * 1024 + 1)
        if reviewer is not None:
            raise ReviewError("A package is already open in this window.", 409)
        reviewer = OfflineReviewer(Path(workspace or Path.home() / ".food-record-reviewer"), data)
        return {"ok": True}

    @app.get("/api/records")
    def queue():
        return require_package().queue()

    @app.get("/api/records/{record_id}")
    def detail(record_id: str):
        return require_package().detail(record_id)

    @app.put("/api/tasks/{task_id}")
    async def save(task_id: str, request: Request):
        body = await request.json()
        return require_package().save(task_id, body.get("revision"), body.get("content"))

    @app.post("/api/tasks/{task_id}/submit")
    async def submit(task_id: str, request: Request):
        body = await request.json()
        return require_package().save(task_id, body.get("revision"), None, submit=True)

    @app.post("/api/tasks/{task_id}/reopen")
    async def reopen(task_id: str, request: Request):
        body = await request.json()
        return require_package().reopen(task_id, body.get("revision"))

    @app.get("/api/offline/result")
    def result():
        return Response(
            require_package().export_result(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="review-results.zip"'},
        )

    @app.get("/api/records/{record_id}/source/{source_id}/{page}")
    def image(record_id: str, source_id: str, page: int):
        d = require_package().detail(record_id)
        document = d["snapshot"]["documents"].get(source_id)
        if not document or not 1 <= page <= document["page_count"]:
            raise ReviewError("Source page not found.", 404)

        preview_hash = reviewer.manifest.get("previews", {}).get(document["sha256"], {}).get(str(page))
        if preview_hash:
            path = reviewer.path / "blobs" / preview_hash
            if sha256(path.read_bytes()) != preview_hash:
                raise ReviewError("Local preview checksum mismatch.")
            return FileResponse(path, media_type="image/png")

        class Store:
            def verified_path(self, digest):
                path = reviewer.path / "blobs" / digest
                if sha256(path.read_bytes()) != digest:
                    raise ReviewError("Local source checksum mismatch.")
                return path

        pages = render_pages(Store(), {"documents": {source_id: document}}, reviewer.path / "rendered", 150)
        return FileResponse(
            next(p["path"] for p in pages if p["page_number"] == page), media_type="image/png"
        )

    return app


def launch(input_path=None, workspace=None, open_browser=True):
    """Desktop entry point; bind an OS-selected loopback port, never a public interface."""
    import socket
    import threading
    import webbrowser

    import uvicorn

    from .offline_reviewer import OfflineReviewer

    root = Path(workspace or Path.home() / ".food-record-reviewer")
    reviewer = OfflineReviewer(root, Path(input_path).read_bytes()) if input_path else None
    app = create_offline_app(reviewer, workspace=root)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}/?launch={app.state.launch_token}"
        print("Offline Reviewer: " + url, flush=True)
        print("Keep this window open while reviewing. Saved work survives closing and reopening.", flush=True)
        if open_browser:
            timer = threading.Timer(0.8, lambda: webbrowser.open(url))
            timer.daemon = True
            timer.start()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False))
        server.run(sockets=[sock])


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Open a review package on this computer; no account or API key needed."
    )
    parser.add_argument("input", type=Path, nargs="?")
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    launch(args.input, args.workspace)
