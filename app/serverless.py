"""Small, stateless application for Vercel Functions.

The legacy local application keeps a SQLite queue and a worker thread.  A
serverless request cannot rely on either, so this module exposes one request
that fetches and returns an existing Xiaoyuzhou transcript.
"""

from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .errors import JobError
from .platform_transcript import fetch_platform_transcript, validate_access_token
from .xiaoyuzhou import fetch_episode, normalize_url

STATIC_DIR = Path(__file__).parent / "static"

class TranscriptRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    token: SecretStr = Field(min_length=1, max_length=16384, repr=False)


def create_serverless_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings(transcript_mode="platform")

    @asynccontextmanager
    async def lifespan(_app):
        yield

    app = FastAPI(title="小宇宙 · 声音留存", version="0.2.0", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

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

    @app.exception_handler(JobError)
    async def job_error_handler(_request, exc):
        status = {
            "PLATFORM_AUTH_REQUIRED": 503,
            "PLATFORM_AUTH_EXPIRED": 401,
            "PLATFORM_AUTH_FORBIDDEN": 403,
        }.get(exc.code, 400)
        return JSONResponse({"error_code": exc.code, "detail": str(exc)}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request, _exc):
        return JSONResponse({"detail": "请填写小宇宙单集链接和 access token。"}, status_code=422)

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "transcript_mode": "platform",
            "auth_mode": "user_supplied",
        }

    @app.post("/api/transcript")
    def transcript(payload: TranscriptRequest):
        url, _episode_id = normalize_url(payload.url)
        token = validate_access_token(payload.token.get_secret_value())
        episode = fetch_episode(url)
        result = fetch_platform_transcript(episode, settings, lambda: None, access_token=token)
        episode_data = episode.as_dict()
        # Audio and internal media identifiers are not needed by the client.
        for key in ("audio_url", "shownotes", "transcript_media_id"):
            episode_data.pop(key, None)
        return {
            "episode": episode_data,
            "transcript_source": "xiaoyuzhou",
            "source_note": "文字来自小宇宙平台已有文稿，未在本机重新识别。",
            "end_timestamps_inferred": result.end_timestamps_inferred,
            "segments": [asdict(segment) for segment in result.segments],
        }

    return app


app = create_serverless_app()
