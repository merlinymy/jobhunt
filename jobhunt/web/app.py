"""The dashboard: a FastAPI JSON API and the React bundle that reads it.

Localhost only. Cross-machine and phone access is Tailscale Serve in front of
this port, never a wider bind address — `serve()` refuses anything that is not
loopback, and that refusal is the whole access-control story, because there is
no auth here.

This module is the shell: lifespan, middleware, error handlers, mounts, and the
SPA fallback. Routing and serialization are in `api.py`, what a page shows is in
`views.py`, and what a button does is in `actions.py`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import db
from . import api

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .. import runs

    # Fail loudly at startup rather than on the first query.
    conn = db.connect()
    try:
        db.verify_schema(conn)
        # A run whose process is gone leaves its row `running`, and the lock is
        # then held by nobody. The common cause is this very restart — `make dev`
        # reloading mid-sweep — so it is worth clearing before the first request
        # rather than waiting out the ten-minute staleness rule.
        reclaimed = runs.reclaim_stale(conn)
    finally:
        conn.close()
    # Say where new postings come from. `uvicorn.error` deliberately: it is the
    # one logger configured by both uvicorn's console default and logconf, so
    # this lands on the terminal under `make dev` and in dashboard.log under
    # launchd. A `jobhunt.*` logger would be silently dropped in dev, which is
    # the same trap that once swallowed uvicorn's own startup banner.
    from .. import doctor

    log = logging.getLogger("uvicorn.error")
    for level, message in doctor.discovery_status():
        log.log(level, message)
    if reclaimed:
        log.warning(
            "discovery: %d run(s) were still marked running from a previous "
            "process and have been closed out as interrupted",
            reclaimed,
        )
    yield

    # On the way down, close out anything this process started. Without it a
    # restart leaves a phantom run holding the lock until its heartbeat ages
    # out, and the button spends ten minutes refusing to do anything.
    try:
        conn = db.connect()
        try:
            runs.interrupt_owned(conn)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - the disk may be what took us down
        pass


app = FastAPI(title="jobhunt", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)

# Order matters and is the real guard: Starlette matches routes in the order they
# are declared, so the catch-all at the bottom of this file cannot shadow either
# of these. The prefix check inside it is the backstop, not the mechanism.
app.include_router(api.router)

if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(DIST / "assets")), name="assets")


@app.middleware("http")
async def same_site_only(request: Request, call_next):
    """Reject cross-site writes.

    There is no auth and no CSRF token here, and the loopback bind was doing all
    the work: any page open in this machine's browser could POST to
    127.0.0.1:8000 and approve, skip, or transition something. Serve keeps the
    port off the wider network, but not away from a local browser tab.

    `None` is allowed through so curl and the launchd smoke checks keep working —
    this raises the floor, it is not a boundary to rely on.
    """
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        site = request.headers.get("sec-fetch-site")
        if site is not None and site not in ("same-origin", "same-site", "none"):
            return PlainTextResponse("cross-site request refused", status_code=403)
    return await call_next(request)


@app.exception_handler(HTTPException)
def http_exception(request: Request, exc: HTTPException) -> Response:
    """`{"error": "<sentence>"}`, so the client has one field to read.

    Not FastAPI's `detail`: every failure, from a stale id to a fabrication
    rejection, arrives through the same parse path. The status code carries the
    meaning — 404 gone, 409 decided elsewhere, 422 rejected.
    """
    return JSONResponse(
        {"error": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers
    )


@app.exception_handler(RequestValidationError)
def validation_error(request: Request, exc: RequestValidationError) -> Response:
    """Pydantic's list of field errors, flattened to the one sentence to show."""
    parts = []
    for error in exc.errors():
        location = ".".join(str(p) for p in error.get("loc", ()) if p != "body")
        parts.append(f"{location}: {error.get('msg')}" if location else str(error.get("msg")))
    return JSONResponse(
        {"error": "; ".join(parts) or "the request body was not valid"}, status_code=422
    )


@app.exception_handler(db.DatabaseLocationError)
def database_unavailable(request: Request, exc: db.DatabaseLocationError) -> Response:
    """503 with the reason, rather than a 500 and a traceback in the log.

    The disk can go away under a running dashboard — a knocked cable is the
    likely one — and every request then fails inside `get_conn`. Being told
    "the external disk is not mounted" from a phone is the difference between a
    thirty-second fix and assuming the app is broken.
    """
    return JSONResponse({"error": str(exc).split("\n")[0]}, status_code=503)


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str = "") -> Response:
    """Serve the React shell for anything that is not an API call or an asset.

    Client-side routing means `/review` and `/packet/12` are real URLs that can
    be reloaded and bookmarked, so every one of them has to return index.html.
    """
    if full_path.startswith(("api/", "assets/")):
        # Without this an unknown endpoint returns index.html with a 200, which
        # looks like "the app loaded but did nothing" instead of a 404.
        raise HTTPException(404, "no such endpoint")

    candidate = (DIST / full_path).resolve()
    if full_path and DIST.resolve() in candidate.parents and candidate.is_file():
        return FileResponse(candidate)  # favicon, and anything else dropped in dist/

    index = DIST / "index.html"
    if not index.is_file():
        return PlainTextResponse(
            "No frontend build here yet. Run `make build-web`.", status_code=503
        )
    # A cached index pointing at hashed assets that no longer exist is the classic
    # white screen after a deploy. The hashed assets themselves can cache forever.
    return FileResponse(index, headers={"Cache-Control": "no-store"})


def _assert_loopback(host: str) -> None:
    """Refuse to bind anywhere but loopback.

    This guard is what makes Tailscale Serve safe: Serve terminates TLS on the
    tailnet and dials 127.0.0.1 from this machine, so the port being unreachable
    any other way is the entire access-control story.

    Resolves hostnames rather than only accepting literals — the old version
    called `ipaddress.ip_address` directly, so `JOBHUNT_HOST=localhost` died with
    a bare ValueError instead of working.
    """
    import ipaddress
    import socket

    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise RuntimeError(f"JOBHUNT_HOST={host!r} does not resolve") from exc
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses or not all(address.is_loopback for address in addresses):
        raise RuntimeError(
            f"refusing to bind {host!r}: the dashboard is loopback only. Reach it "
            "from other machines over Tailscale Serve, which proxies to this port."
        )


def serve(*, reload: bool = False, wait_for_db: float = 0.0) -> None:
    """Run the dashboard. Production by default; `--reload` is the opt-in.

    `reload` used to be hardcoded on, and the launchd agent ran this same
    function — so the always-on dashboard was running uvicorn's dev auto-reloader
    with `watchfiles` not installed, meaning a stat-poll of the whole repo every
    quarter second, two processes, and a restart mid-request on any `git pull`.
    The default is now the safe one: forgetting the flag gets you production.
    """
    import uvicorn
    from uvicorn.config import LOGGING_CONFIG

    from .. import config, doctor
    from . import logconf

    _assert_loopback(config.HOST)
    if wait_for_db:
        # KeepAlive is a plain true, so without this an unmounted disk at login
        # means a traceback every ThrottleInterval forever. One sleeper instead,
        # which comes back on its own the moment the volume appears.
        doctor.wait_for_db(wait_for_db)
    uvicorn.run(
        "jobhunt.web.app:app",
        host=config.HOST,
        port=config.PORT,
        reload=reload,
        # The reloader wants the console; a daemon wants its own rotating file.
        # NOT None for the console case: uvicorn reads None as "configure no
        # logging at all", so its handlers go missing and every startup line —
        # INFO, below the logging.lastResort WARNING floor — is dropped. `make
        # dev` then prints nothing at all while serving perfectly well.
        log_config=LOGGING_CONFIG if reload else logconf.dict_config(),
        # Tailscale Serve sets X-Forwarded-*; trust it only from the local proxy.
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        timeout_graceful_shutdown=10,
        server_header=False,
    )


def run_dev() -> None:
    """`make dev`. Auto-reload; never what the launchd agent runs."""
    serve(reload=True)


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="run the jobhunt dashboard")
    parser.add_argument("--reload", action="store_true", help="dev auto-reload")
    parser.add_argument(
        "--wait-for-db", type=float, default=0.0, metavar="SECONDS",
        help="wait for the database volume before binding (launchd uses this)",
    )
    args = parser.parse_args(argv)
    serve(reload=args.reload, wait_for_db=args.wait_for_db)


if __name__ == "__main__":
    main()
