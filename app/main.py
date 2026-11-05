import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .db import Store
from .errors import JobError
from .local_options import estimate_asr, local_options, resolve_config
from .platform_transcript import check_access_token, credentials_configured, save_access_token
from .worker import Worker
from .xiaoyuzhou import fetch_episode, normalize_url

STATIC_DIR = Path(__file__).parent / "static"
FILE_MIME = {
    "txt": "text/plain",
    "md": "text/markdown",
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
    "json": "application/json",
}


class LocalConfigRequest(BaseModel):
    model: str | None = Field(default=None, min_length=1, max_length=512)
    device: Literal["auto", "cpu", "cuda"] | None = None
    compute_type: Literal["auto", "int8", "float32", "float16", "int8_float16"] | None = None


class CreateJobRequest(LocalConfigRequest):
    url: str = Field(min_length=1, max_length=4096)
    language: Literal["zh", "en", "auto"] = "zh"
    transcript_mode: Literal["platform", "local"] | None = None
    download_audio: bool = False


class CheckTokenRequest(BaseModel):
    url: str | None = Field(default=None, max_length=4096)


class SaveTokenRequest(BaseModel):
    token: SecretStr = Field(repr=False)


class EstimateRequest(LocalConfigRequest):
    url: str | None = Field(default=None, max_length=4096)


def serialize_job(job, settings):
    files = {}
    for key, filename in (job.get("files_json") or {}).items():
        if (settings.storage_dir / job["id"] / filename).is_file():
            files[key] = f"/api/jobs/{job['id']}/files/{key}"
    episode = dict(job["episode_json"]) if job["episode_json"] else None
    if episode:
        episode.pop("audio_url", None)
        episode.pop("shownotes", None)
        episode.pop("transcript_media_id", None)
    return {
        "job_id": job["id"],
        "status": job["status"],
        "stage": job["stage"],
        "progress": job["progress"],
        "error_code": job["error_code"],
        "error_message": job["error_message"],
        "episode": episode,
        "files": files,
        "language": job["language"],
        "model": job["model"],
        "device": job.get("device") or settings.whisper_device,
        "compute_type": job.get("compute_type") or settings.whisper_compute_type,
        "asr_seconds": job.get("asr_seconds"),
        "transcript_mode": job.get("transcript_mode", "local"),
        "download_audio": bool(job.get("download_audio", True)),
        "transcript_source": job.get("transcript_source"),
        "source_note": job.get("source_note"),
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }


def create_app(settings: Settings | None = None, worker_enabled=True):
    settings = settings or Settings()
    store = Store(settings.db_path)
    worker = Worker(settings, store)

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        if worker_enabled:
            worker.start()
        yield
        if worker_enabled:
            await asyncio.to_thread(worker.stop)

    app = FastAPI(title="小宇宙 · 声音留存", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.worker, app.state.settings = store, worker, settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method in {"POST", "PUT", "DELETE", "PATCH"} and origin:
            if urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "不允许跨站提交任务"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path not in {"/docs", "/redoc", "/docs/oauth2-redirect"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' https:; media-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
            )
        return response

    @app.exception_handler(JobError)
    async def job_error_handler(request, exc):
        return JSONResponse(
            {"error_code": exc.code, "detail": str(exc)},
            status_code={"QUEUE_FULL": 429, "PLATFORM_AUTH_ENV_OVERRIDE": 409}.get(exc.code, 400),
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request, exc):
        if request.url.path == "/api/platform/token":
            # Pydantic errors can echo raw request input; never reflect credentials.
            return JSONResponse(
                {"detail": "请以 JSON 提交字符串类型的 access token。"}, status_code=422,
                headers={"Cache-Control": "no-store"},
            )
        from fastapi.exception_handlers import request_validation_exception_handler

        return await request_validation_exception_handler(request, exc)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "worker_running": worker.thread.is_alive(),
            "model": settings.whisper_model,
            "device": settings.whisper_device,
            "transcript_mode": "local" if settings.transcript_mode == "local" else "platform",
            "platform_credentials_configured": credentials_configured(settings),
        }

    @app.get("/api/jobs")
    def recent_jobs():
        return [serialize_job(job, settings) for job in store.recent()]

    @app.post("/api/platform/check-token")
    def check_token(payload: CheckTokenRequest):
        url = normalize_url(payload.url)[0] if payload.url and payload.url.strip() else None
        return JSONResponse(check_access_token(settings, url), headers={"Cache-Control": "no-store"})

    @app.post("/api/platform/token")
    def save_token(payload: SaveTokenRequest):
        save_access_token(settings, payload.token.get_secret_value())
        return JSONResponse(
            {"status": "saved", "message": "access token 已保存在本机，立即生效。"},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/local/options")
    def get_local_options():
        return local_options(settings)

    @app.post("/api/local/estimate")
    def estimate(payload: EstimateRequest):
        config = resolve_config(settings, payload.model, payload.device, payload.compute_type)
        episode = fetch_episode(normalize_url(payload.url)[0]) if payload.url and payload.url.strip() else None
        duration = episode.duration if episode else 1800
        if not duration or duration <= 0:
            raise JobError("DURATION_UNKNOWN", "无法获取节目时长，暂时无法估算识别时间。")
        return {
            **config, "duration": duration, "example": episode is None,
            "title": episode.title if episode else None, **estimate_asr(store, config, duration),
        }

    def require_platform_access(url):
        result = check_access_token(settings, url)
        if result["status"] != "valid":
            code = {"missing": 401, "invalid": 401, "rejected": 401, "forbidden": 403,
                    "no_transcript": 400}.get(result["status"], 503)
            raise HTTPException(code, result["message"] + " 平台文稿模式必须验证通过后才能提交。")

    @app.post("/api/jobs", status_code=202)
    def submit(payload: CreateJobRequest):
        url, episode_id = normalize_url(payload.url)
        mode = payload.transcript_mode or ("local" if settings.transcript_mode == "local" else "platform")
        config = {"model": settings.whisper_model, "device": None, "compute_type": None}
        if mode == "platform":
            require_platform_access(url)
        else:
            config = resolve_config(settings, payload.model, payload.device, payload.compute_type)
        job, reused = store.enqueue(
            url, episode_id, payload.language, config["model"], settings.max_queued_jobs,
            transcript_mode=mode,
            download_audio=payload.download_audio if mode == "platform" else True,
            device=config["device"], compute_type=config["compute_type"],
        )
        if job["status"] == "COMPLETED":
            present = {
                kind: name
                for kind, name in (job["files_json"] or {}).items()
                if (settings.storage_dir / job["id"] / name).is_file()
            }
            expected = {"txt", "md", "srt", "vtt", "json"}
            if job.get("download_audio", True) or job.get("transcript_source") != "xiaoyuzhou":
                expected.add("audio")
            if not expected.issubset(present):
                store.reset_missing_outputs(
                    job["id"], {"audio": present["audio"]} if "audio" in present else {}
                )
                job = store.get(job["id"])
        worker.notify()
        return {**serialize_job(job, settings), "reused": reused}

    def require_job(job_id):
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise HTTPException(404, "任务不存在")
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        return job

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return serialize_job(require_job(job_id), settings)

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str):
        job = require_job(job_id)
        if job["status"] not in {"FAILED", "CANCELLED"}:
            raise HTTPException(409, "只有失败或已终止的任务可以重新提交")
        if job.get("transcript_mode", "local") != "local":
            require_platform_access(job["url"])
        if not store.retry(job_id):
            raise HTTPException(409, "任务状态已变化，请刷新后再试")
        worker.notify()
        return serialize_job(store.get(job_id), settings)

    @app.post("/api/jobs/{job_id}/cancel", status_code=202)
    def cancel(job_id: str):
        require_job(job_id)
        status = store.cancel(job_id)
        if status is None:
            raise HTTPException(404, "任务不存在")
        if status == "CANCEL_REQUESTED":
            worker.request_cancel(job_id)
        elif status != "CANCELLED":
            raise HTTPException(409, "只有排队中或处理中的任务可以终止")
        return serialize_job(store.get(job_id), settings)

    @app.get("/api/jobs/{job_id}/files/{kind}")
    def download(job_id: str, kind: str):
        job = require_job(job_id)
        filename = (job["files_json"] or {}).get(kind)
        if not filename:
            raise HTTPException(404, "文件尚未生成")
        directory = (settings.storage_dir / job_id).resolve()
        path = (directory / filename).resolve()
        if path.parent != directory or not path.is_file():
            raise HTTPException(404, "文件已被清理")
        title = (job["episode_json"] or {}).get("title") or "podcast"
        title = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", title).strip(". ")[:100] or "podcast"
        return FileResponse(path, filename=f"{title}{path.suffix}", media_type=FILE_MIME.get(kind))

    @app.get("/api/jobs/{job_id}/transcript")
    def preview(job_id: str):
        job = require_job(job_id)
        filename = (job["files_json"] or {}).get("json")
        if not filename:
            raise HTTPException(404, "转写尚未完成")
        path = settings.storage_dir / job_id / filename
        if not path.is_file():
            raise HTTPException(404, "文件已被清理")
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    return app


app = create_app()
