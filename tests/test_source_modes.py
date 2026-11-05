import sys
from types import SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient

from app.local_options import estimate_asr, resolve_config
from app.main import create_app
from app.transcriber import FasterWhisperTranscriber
from conftest import EPISODE_ID, URL
from test_workflow import install_fake_dependencies
from test_platform_transcript import CDN, TOKEN, mock_http


@pytest.fixture(autouse=True)
def cpu_machine(monkeypatch):
    monkeypatch.setattr("app.local_options.runtime_capabilities", lambda: {"cpu": ["int8", "float32"]})


@pytest.mark.parametrize(("status", "expected"), [
    ("missing", 401), ("invalid", 401), ("rejected", 401), ("forbidden", 403),
    ("no_transcript", 400), ("unknown", 503), ("unchecked", 503),
])
def test_platform_submit_strictly_rejects_unverified_credentials(settings, monkeypatch, status, expected):
    monkeypatch.setattr("app.main.check_access_token", lambda *args: {"status": status, "message": "未通过验证"})
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        response = client.post("/api/jobs", json={"url": URL, "transcript_mode": "platform"})
        assert response.status_code == expected
        assert client.get("/api/jobs").json() == []


def test_platform_submit_with_missing_token_never_enqueues(settings):
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        response = client.post("/api/jobs", json={"url": URL, "transcript_mode": "platform"})
        assert response.status_code == 401
        assert client.get("/api/jobs").json() == []


def test_token_expiring_after_preflight_is_rejected_again_on_submit(settings, episode, monkeypatch):
    settings.xiaoyuzhou_access_token = TOKEN
    episode.transcript_media_id = "test/audio.m4a"
    monkeypatch.setattr("app.xiaoyuzhou.fetch_episode", lambda url: episode)
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"data": {"transcriptUrl": CDN}})
        return httpx.Response(401, text=TOKEN)

    mock_http(monkeypatch, handle)
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        assert client.post("/api/platform/check-token", json={"url": URL}).json()["status"] == "valid"
        response = client.post("/api/jobs", json={"url": URL, "transcript_mode": "platform"})
        assert response.status_code == 401
        assert TOKEN not in response.text
        assert client.get("/api/jobs").json() == []
        assert len(requests) == 2


def test_platform_rechecks_on_submit_reuse_and_retry(settings, monkeypatch):
    calls = []
    state = {"status": "valid", "message": "验证通过"}

    def check(settings, url):
        calls.append(url)
        return state

    monkeypatch.setattr("app.main.check_access_token", check)
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        payload = {"url": URL, "transcript_mode": "platform"}
        first = client.post("/api/jobs", json=payload)
        assert first.status_code == 202
        job_id = first.json()["job_id"]
        assert first.json()["transcript_mode"] == "platform"
        state["status"] = "rejected"
        assert client.post("/api/jobs", json=payload).status_code == 401
        client.app.state.store.update(job_id, status="FAILED")
        assert client.post(f"/api/jobs/{job_id}/retry").status_code == 401
        assert client.app.state.store.get(job_id)["status"] == "FAILED"
        state["status"] = "valid"
        assert client.post(f"/api/jobs/{job_id}/retry").status_code == 202
        assert len(calls) == 4


def test_auto_mode_is_not_accepted_for_new_tasks(settings):
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        assert client.post("/api/jobs", json={"url": URL, "transcript_mode": "auto"}).status_code == 422
        assert client.get("/api/jobs").json() == []


def test_local_configuration_is_persisted_deduplicated_and_used_by_worker(settings, monkeypatch, episode, wav_bytes):
    def no_token(*args):
        pytest.fail("Local mode must not need token validation")

    monkeypatch.setattr("app.main.check_access_token", no_token)
    app = create_app(settings, worker_enabled=False)
    install_fake_dependencies(monkeypatch, episode, wav_bytes, app.state.worker)
    loaded = []
    monkeypatch.setattr(app.state.worker.transcriber, "load_model", lambda model: loaded.append(
        (model, app.state.worker.transcriber.device, app.state.worker.transcriber.compute_type)
    ))
    with TestClient(app) as client:
        payload = {"url": URL, "transcript_mode": "local", "model": "base", "device": "cpu",
                   "compute_type": "float32", "download_audio": False, "language": "en"}
        first = client.post("/api/jobs", json=payload)
        assert first.status_code == 202
        data = first.json()
        assert (data["model"], data["device"], data["compute_type"], data["language"]) == (
            "base", "cpu", "float32", "en",
        )
        assert data["download_audio"] is True
        assert client.post("/api/jobs", json=payload).json()["job_id"] == data["job_id"]
        other = client.post("/api/jobs", json={**payload, "compute_type": "int8"}).json()
        assert other["job_id"] != data["job_id"]
        app.state.worker.process(app.state.store.claim())
        assert loaded == [("base", "cpu", "float32")]
        finished = client.get(f"/api/jobs/{data['job_id']}").json()
        assert finished["status"] == "COMPLETED"
        assert finished["asr_seconds"] > 0


@pytest.mark.parametrize("config", [
    {"model": "invalid-model"}, {"device": "cuda"}, {"device": "cpu", "compute_type": "float16"},
])
def test_invalid_local_configuration_is_rejected_before_queueing(settings, config):
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        response = client.post("/api/jobs", json={"url": URL, "transcript_mode": "local", **config})
        assert response.status_code == 400
        assert response.json()["error_code"] == "LOCAL_CONFIG_INVALID"
        assert client.get("/api/jobs").json() == []


def test_options_and_estimate_use_metadata_without_downloading_or_enqueuing(settings, episode, monkeypatch):
    episode.duration = 1200
    monkeypatch.setattr("app.main.fetch_episode", lambda url: episode)
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        options = client.get("/api/local/options").json()
        assert "cuda" not in options["devices"]
        assert {model["value"] for model in options["models"]} >= {"tiny", "small", "medium"}
        payload = {"url": URL, "model": "small", "device": "cpu", "compute_type": "int8"}
        estimate = client.post("/api/local/estimate", json=payload).json()
        assert estimate["duration"] == 1200
        assert estimate["example"] is False
        assert 0 < estimate["min_seconds"] < estimate["max_seconds"]
        assert estimate["basis"] == "rough"
        assert estimate["sample_count"] == 0
        sample = client.post("/api/local/estimate", json={}).json()
        assert sample["example"] is True
        assert sample["duration"] == 1800
        assert client.get("/api/jobs").json() == []


def test_timing_estimate_prefers_same_configuration_history_and_scales_with_duration(settings, store):
    job = store.enqueue(URL, EPISODE_ID, "zh", "small", device="cpu", compute_type="int8")[0]
    store.update(job["id"], status="COMPLETED", transcript_source="local",
                 episode_json={"duration": 600}, asr_seconds=60)
    config = {"model": "small", "device": "cpu", "compute_type": "int8"}
    result = estimate_asr(store, config, 1200)
    assert result["basis"] == "history"
    assert result["sample_count"] == 1
    assert result["min_seconds"] == 84
    assert result["max_seconds"] == 180
    assert estimate_asr(store, {**config, "compute_type": "float32"}, 1200)["basis"] == "rough"
    assert estimate_asr(store, {**config, "model": "base"}, 1200)["basis"] == "rough"


def test_model_cache_changes_when_device_or_precision_changes(settings, monkeypatch):
    loaded = []

    def model(name, **kwargs):
        loaded.append((name, kwargs["device"], kwargs["compute_type"]))
        return object()

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=model))
    transcriber = FasterWhisperTranscriber(settings)
    transcriber.configure("cpu", "int8")
    transcriber.load_model("tiny")
    transcriber.load_model("tiny")
    transcriber.configure("cpu", "float32")
    transcriber.load_model("tiny")
    transcriber.configure("cuda", "float16")
    transcriber.load_model("tiny")
    assert loaded == [("tiny", "cpu", "int8"), ("tiny", "cpu", "float32"), ("tiny", "cuda", "float16")]


def test_automatic_settings_resolve_before_persisting(settings, monkeypatch):
    monkeypatch.setattr("app.local_options.runtime_capabilities", lambda: {
        "cpu": ["int8", "float32"], "cuda": ["float16", "int8"],
    })
    assert resolve_config(settings, device="auto", compute_type="auto") == {
        "model": "tiny", "device": "cuda", "compute_type": "float16",
    }
