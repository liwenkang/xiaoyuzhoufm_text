"""FastAPI entrypoint using server-only shared credentials."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .errors import AppError
from .shared_transcript import SharedTranscriptService
from .xiaoyuzhou import normalize_url

PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"


class TranscriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=4096)


def create_serverless_app(service=None) -> FastAPI:
    app = FastAPI(title="小宇宙 · 声音留存", version="0.2.0")
    shared_service = service if service is not None else SharedTranscriptService()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method == "POST" and origin:
            host = request.headers.get("host", "")
            if origin.split("://", 1)[-1].split("/", 1)[0] != host:
                return JSONResponse({"detail": "不允许跨站提交请求"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "public, max-age=300"
        if request.url.path not in {"/docs", "/redoc", "/docs/oauth2-redirect"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' https:; media-src 'none'; "
                "script-src 'self'; style-src 'self'; object-src 'none'; "
                "frame-ancestors 'none'; base-uri 'self'"
            )
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(_request, exc):
        status = {
            "PLATFORM_AUTH_EXPIRED": 401,
            "PLATFORM_AUTH_FORBIDDEN": 403,
            "SHARED_RATE_LIMITED": 429,
            "SHARED_AUTH_UNCONFIGURED": 503,
            "SHARED_AUTH_RECOVERY_REQUIRED": 503,
            "SHARED_AUTH_REFRESH_FAILED": 503,
            "SHARED_STORE_UNAVAILABLE": 503,
            "SHARED_SERVICE_BUSY": 503,
        }.get(exc.code, 400)
        headers = {"Retry-After": "60"} if status == 429 else {}
        if exc.code == "SHARED_SERVICE_BUSY":
            headers["Retry-After"] = "5"
        return JSONResponse({"error_code": exc.code, "detail": str(exc)}, status_code=status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request, _exc):
        return JSONResponse({"detail": "请仅提交有效的小宇宙单集链接。"}, status_code=422)

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "transcript_mode": "platform",
            "auth_mode": "server_shared",
        }

    @app.post("/api/transcript")
    def transcript(payload: TranscriptRequest):
        url, episode_id = normalize_url(payload.url)
        return shared_service.fetch(url, episode_id)

    # Vercel serves public/ from its CDN and excludes it from the Function.
    # Mount the same directory last for local development, after API routes.
    if PUBLIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="frontend")

    return app


app = create_serverless_app()
