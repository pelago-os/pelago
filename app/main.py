from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.database import get_connection, init_schema
from app.routes.forum import router as forum_router
from app.routes.library import router as library_router
from app.routes.station import router as station_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    with get_connection() as conn:
        init_schema(conn)
    from app.services.audit import recover_mutation_audits
    recover_mutation_audits()
    yield


app = FastAPI(title="Pelago OS", version="0.1.0", lifespan=lifespan)
app.include_router(forum_router)
app.include_router(station_router)
app.include_router(library_router)


# 审查 S6（2026-10）：这几条不会影响现有页面，直接生效
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
}
# 目标策略先只报告不拦截：前端还有内联 onclick，改掉之后再转为强制
CSP_REPORT_ONLY = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' https: data: blob:; media-src 'self' blob:; connect-src 'self'; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)


@app.middleware("http")
async def no_cache_static_assets(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    response.headers.setdefault("Content-Security-Policy-Report-Only", CSP_REPORT_ONLY)
    return response

# Serve frontend/ directly; point PELAGO_STATIC_DIR at a built release (scripts/build_frontend_release.py) to cache-bust.
static_dir = config.settings.static_dir
app.mount("/static", StaticFiles(directory=static_dir), name="static")


from app.services.upload_access import UploadFiles


app.mount("/uploads", UploadFiles(check_dir=False), name="uploads")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "pelago-os"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str) -> FileResponse:
    first_segment = full_path.split("/", 1)[0]
    if first_segment in {"api", "static", "uploads", "health"}:
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(static_dir / "index.html")
