from concurrent.futures import ThreadPoolExecutor
import threading
import time
import pytest
from fastapi.testclient import TestClient

from app.errors import JobError
from app.main import create_app
from app.transcriber import Segment, timestamp
from app.worker import Worker
from conftest import EPISODE_ID, URL


def enqueue(store):
    return store.enqueue(URL, EPISODE_ID, "zh", "tiny")[0]


def install_fake_dependencies(monkeypatch, episode, wav_bytes, worker, fail_asr=False):
    monkeypatch.setattr("app.worker.fetch_episode", lambda url: episode)

    def fake_download(ep, directory, settings, progress, stop):
        path = directory / "audio.wav"
        path.write_bytes(wav_bytes)
        return path

    monkeypatch.setattr("app.worker.download_audio", fake_download)
    monkeypatch.setattr(worker.transcriber, "load_model", lambda name: None)

    def fake_transcribe(*args):
        if fail_asr:
            raise JobError("ASR_FAILED", "测试转写失败")
        return [Segment(0, 0.5, "你好，世界。"), Segment(0.5, 1, "第二句话。")], "zh"

    monkeypatch.setattr(worker.transcriber, "transcribe", fake_transcribe)


def test_complete_workflow_downloads_and_utf8_preview(settings, monkeypatch, episode, wav_bytes):
    app = create_app(settings, worker_enabled=False)
    with TestClient(app) as client:
        response = client.post("/api/jobs", json={"url": URL})
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        worker = app.state.worker
        install_fake_dependencies(monkeypatch, episode, wav_bytes, worker)
        worker.process(app.state.store.claim())
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "COMPLETED"
        assert set(job["files"]) == {"audio", "txt", "md", "srt", "vtt", "json"}
        for url in job["files"].values():
            download = client.get(url)
            assert download.status_code == 200
            assert "attachment" in download.headers["content-disposition"]
        assert "你好，世界。" in client.get(job["files"]["txt"]).content.decode("utf-8")
        transcript = client.get(f"/api/jobs/{job_id}/transcript").json()
        assert transcript["segments"][1]["start"] == 0.5
        assert transcript["episode_id"] == EPISODE_ID
        assert client.get(job["files"]["vtt"]).text.startswith("WEBVTT")
        assert "00:00:00,000 --> 00:00:00,500" in client.get(job["files"]["srt"]).text
        reused = client.post("/api/jobs", json={"url": URL + "?share=1"}).json()
        assert reused["reused"] is True
        assert reused["job_id"] == job_id


def test_asr_failure_keeps_audio_and_retry_reuses_download(settings, store, monkeypatch, episode, wav_bytes):
    job = enqueue(store)
    worker = Worker(settings, store)
    install_fake_dependencies(monkeypatch, episode, wav_bytes, worker, fail_asr=True)
    worker.process(store.claim())
    failed = store.get(job["id"])
    assert failed["status"] == "FAILED"
    assert (settings.storage_dir / job["id"] / failed["files_json"]["audio"]).exists()
    assert store.retry(job["id"])

    def no_redownload(*args):
        pytest.fail("Retry must reuse completed audio")

    monkeypatch.setattr("app.worker.download_audio", no_redownload)
    monkeypatch.setattr(worker.transcriber, "transcribe", lambda *args: ([Segment(0, 1, "重试成功")], "zh"))
    worker.process(store.claim())
    assert store.get(job["id"])["status"] == "COMPLETED"


def test_restart_recovers_and_claims_once(store):
    job = enqueue(store)
    assert store.claim()["id"] == job["id"]
    assert store.claim() is None
    store.recover()
    assert store.get(job["id"])["status"] == "QUEUED"
    assert store.claim()["id"] == job["id"]


def test_concurrent_duplicate_requests_create_one_job(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: enqueue(store)["id"], range(12)))
    assert len(set(results)) == 1
    assert len(store.recent()) == 1


def test_error_status_and_cross_site_submission(settings):
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        assert client.post("/api/jobs", json={"url": "https://127.0.0.1/a"}).status_code == 400
        assert client.post("/api/jobs", json={"url": URL, "language": "invalid"}).status_code == 422
        assert (
            client.post("/api/jobs", json={"url": URL}, headers={"Origin": "https://evil.test"}).status_code
            == 403
        )
        assert client.get("/api/jobs/../secret").status_code == 404
        assert client.get("/api/jobs/" + "0" * 32).status_code == 404
        assert client.get("/", headers={"Host": "evil.test"}).status_code == 400


def test_cancel_queued_job_and_retry(settings):
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        created = client.post("/api/jobs", json={"url": URL}).json()
        job_id = created["job_id"]
        cancelled = client.post(f"/api/jobs/{job_id}/cancel", json={})
        assert cancelled.status_code == 202
        assert cancelled.json()["status"] == "CANCELLED"
        retried = client.post(f"/api/jobs/{job_id}/retry", json={})
        assert retried.status_code == 202
        assert retried.json()["status"] == "QUEUED"


def test_cancel_running_job_keeps_downloaded_audio(settings, store, monkeypatch, episode, wav_bytes):
    job = enqueue(store)
    worker = Worker(settings, store)
    started = threading.Event()
    install_fake_dependencies(monkeypatch, episode, wav_bytes, worker)

    def slow_transcribe(audio, language, duration, title, on_progress, check_stop):
        started.set()
        while True:
            check_stop()
            time.sleep(0.01)

    monkeypatch.setattr(worker.transcriber, "transcribe", slow_transcribe)
    claimed = store.claim()
    thread = threading.Thread(target=worker.process, args=(claimed,))
    thread.start()
    assert started.wait(2)
    assert store.cancel(job["id"]) == "CANCEL_REQUESTED"
    worker.request_cancel(job["id"])
    thread.join(2)
    cancelled = store.get(job["id"])
    assert not thread.is_alive()
    assert cancelled["status"] == "CANCELLED"
    assert (settings.storage_dir / job["id"] / cancelled["files_json"]["audio"]).exists()


def test_download_path_cannot_escape_job_directory(settings, store):
    job = enqueue(store)
    secret = settings.storage_dir / "secret.txt"
    secret.write_text("secret")
    store.update(job["id"], files_json={"txt": "../secret.txt"})
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        assert client.get(f"/api/jobs/{job['id']}/files/txt").status_code == 404


def test_timestamp_rounding_across_hour_boundary():
    assert timestamp(3599.9999) == "01:00:00,000"


def test_completed_task_with_missing_files_can_be_regenerated(settings):
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        job = client.post("/api/jobs", json={"url": URL}).json()
        client.app.state.store.update(job["job_id"], status="COMPLETED", files_json={"txt": "transcript.txt"})
        result = client.post("/api/jobs", json={"url": URL}).json()
        assert result["job_id"] == job["job_id"]
        assert result["status"] == "QUEUED"
        assert result["files"] == {}


def test_only_one_worker_can_share_storage(settings):
    from app.lock import WorkerLock

    first = WorkerLock(settings.storage_dir / "test.lock")
    second = WorkerLock(settings.storage_dir / "test.lock")
    first.acquire()
    try:
        with pytest.raises(RuntimeError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_background_worker_recovers_executes_and_stops(settings, store, monkeypatch, episode, wav_bytes):
    import time

    job = enqueue(store)
    store.claim()  # Simulate a previous process stopping while this job was RUNNING.
    worker = Worker(settings, store)
    install_fake_dependencies(monkeypatch, episode, wav_bytes, worker)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and store.get(job["id"])["status"] != "COMPLETED":
            time.sleep(0.02)
        assert store.get(job["id"])["status"] == "COMPLETED"
    finally:
        worker.stop()
    assert not worker.thread.is_alive()


def test_existing_database_migration_preserves_jobs_and_is_idempotent(settings):
    import sqlite3
    from app.db import Store

    with sqlite3.connect(settings.db_path) as db:
        db.execute("""CREATE TABLE jobs (
            id TEXT PRIMARY KEY, url TEXT NOT NULL, episode_id TEXT NOT NULL,
            language TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
            stage TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
            error_code TEXT, error_message TEXT, episode_json TEXT, files_json TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        db.execute(
            "INSERT INTO jobs (id,url,episode_id,language,model,status,stage,progress,created_at,updated_at) "
            "VALUES ('legacy',?,?,'zh','small','COMPLETED','DONE',100,'old','old')", (URL, EPISODE_ID)
        )
    migrated = Store(settings.db_path)
    migrated.initialize()
    migrated.initialize()
    legacy = migrated.get("legacy")
    assert legacy["status"] == "COMPLETED"
    assert legacy["transcript_mode"] == "local"
    assert legacy["download_audio"] == 1
    assert legacy["device"] is None
    assert legacy["compute_type"] is None
    assert legacy["asr_seconds"] is None
