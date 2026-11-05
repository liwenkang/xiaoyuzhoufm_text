import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from app import platform_transcript as platform
from app.errors import JobError, JobInterrupted
from app.main import create_app
from app.transcriber import Segment
from app.worker import Worker
from conftest import EPISODE_ID, URL


TOKEN = "test-operator-token"
CDN = "https://media.xyzcdn.net/example/transcript.json?signature=test"
HTTP_CLIENT = httpx.Client


def mock_http(monkeypatch, handler):
    monkeypatch.setattr(
        platform.httpx, "Client", lambda **kwargs: HTTP_CLIENT(transport=httpx.MockTransport(handler), **kwargs)
    )
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )


def ready(settings, episode):
    settings.xiaoyuzhou_access_token = TOKEN
    episode.transcript_media_id = "podcast-id/audio.m4a"
    episode.duration = 12


def test_reads_authenticated_address_then_cdn_without_credentials(settings, episode, monkeypatch):
    ready(settings, episode)
    requests = []

    def handle(request):
        requests.append(request)
        if str(request.url) == platform.API_URL:
            assert request.method == "POST"
            assert request.headers["x-jike-access-token"] == TOKEN
            assert json.loads(request.content) == {"eid": EPISODE_ID, "mediaId": episode.transcript_media_id}
            return httpx.Response(
                200, json={"data": {"transcriptUrl": CDN}}, headers={"set-cookie": "api-secret=test; Domain=.xyzcdn.net"}
            )
        assert "x-jike-access-token" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, json=[{"text": "你好", "startMs": 1500}, {"text": "世界", "startMs": 4000}])

    mock_http(monkeypatch, handle)
    result = platform.fetch_platform_transcript(episode, settings, lambda: None)
    assert result.segments == [Segment(1.5, 4, "你好"), Segment(4, 12, "世界")]
    assert result.end_timestamps_inferred is True
    assert len(requests) == 2


def test_end_times_preserved_and_blank_rows_ignored():
    result = platform.parse_segments([
        {"text": " ", "startMs": 0},
        {"text": "第一段", "startMs": 100, "endMs": 850},
        {"text": "第二段", "startMs": 1000, "endMs": 1900},
    ], 2)
    assert result.segments == [Segment(0.1, 0.85, "第一段"), Segment(1, 1.9, "第二段")]
    assert result.end_timestamps_inferred is False


@pytest.mark.parametrize("payload", [
    [], {"segments": []}, [{"text": "丢时间戳"}],
    [{"text": "错误单位", "start": 0}], [{"text": "坏数据", "startMs": -1}],
    [{"text": "坏数据", "startMs": True}], [{"text": "坏数据", "startMs": float("nan")}],
    [{"text": "倒序", "startMs": 1000}, {"text": "倒序", "startMs": 500}],
    [{"text": "结束错误", "startMs": 1000, "endMs": 1000}],
    [{"text": "超时长", "startMs": 3000}],
    [{"text": "部分有效", "startMs": 0}, {"text": 12, "startMs": 1000}],
])
def test_invalid_transcript_never_silently_becomes_success(payload):
    with pytest.raises(JobError) as error:
        platform.parse_segments(payload, 2)
    assert error.value.code == "PLATFORM_TRANSCRIPT_INVALID"


@pytest.mark.parametrize("status", [401, 403])
def test_expired_token_has_actionable_sanitized_error(settings, episode, monkeypatch, status):
    ready(settings, episode)
    mock_http(monkeypatch, lambda request: httpx.Response(status, text=TOKEN))
    with pytest.raises(JobError) as error:
        platform.fetch_platform_transcript(episode, settings, lambda: None)
    assert error.value.code == ("PLATFORM_AUTH_EXPIRED" if status == 401 else "PLATFORM_AUTH_FORBIDDEN")
    assert TOKEN not in str(error.value)


def test_api_redirect_is_not_followed(settings, episode, monkeypatch):
    ready(settings, episode)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://evil.test/steal"})

    mock_http(monkeypatch, handle)
    with pytest.raises(JobError):
        platform.fetch_platform_transcript(episode, settings, lambda: None)
    assert len(requests) == 1


@pytest.mark.parametrize("url", ["https://evil.test/a", "http://media.xyzcdn.net/a", "https://127.0.0.1/a"])
def test_cdn_address_rejected_before_request(settings, episode, monkeypatch, url):
    ready(settings, episode)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"transcriptUrl": url}})

    mock_http(monkeypatch, handle)
    with pytest.raises(JobError) as error:
        platform.fetch_platform_transcript(episode, settings, lambda: None)
    assert error.value.code == "PLATFORM_TRANSCRIPT_UNSAFE_URL"
    assert len(requests) == 1


def test_cdn_redirect_revalidated(settings, episode, monkeypatch):
    ready(settings, episode)
    requests = []

    def handle(request):
        requests.append(request)
        if str(request.url) == platform.API_URL:
            return httpx.Response(200, json={"data": {"transcriptUrl": CDN}})
        return httpx.Response(302, headers={"location": "https://evil.test/a"})

    mock_http(monkeypatch, handle)
    with pytest.raises(JobError) as error:
        platform.fetch_platform_transcript(episode, settings, lambda: None)
    assert error.value.code == "PLATFORM_TRANSCRIPT_UNSAFE_URL"
    assert len(requests) == 2


def test_response_limit_and_network_errors_are_sanitized(settings, episode, monkeypatch):
    ready(settings, episode)
    settings.max_transcript_bytes = 10

    def handle(request):
        if str(request.url) == platform.API_URL:
            return httpx.Response(200, json={"data": {"transcriptUrl": CDN}})
        return httpx.Response(200, content=b"x" * 11)

    mock_http(monkeypatch, handle)
    with pytest.raises(JobError) as error:
        platform.fetch_platform_transcript(episode, settings, lambda: None)
    assert error.value.code == "PLATFORM_TRANSCRIPT_INVALID"

    def failure(request):
        raise httpx.ReadTimeout(TOKEN + CDN)

    mock_http(monkeypatch, failure)
    with pytest.raises(JobError) as error:
        platform.fetch_platform_transcript(episode, settings, lambda: None)
    assert TOKEN not in str(error.value)
    assert CDN not in str(error.value)


def test_token_file_can_be_updated_without_restart_and_stays_out_of_repr(settings):
    settings.xiaoyuzhou_token_file.write_text(TOKEN, encoding="utf-8-sig")
    assert platform.read_access_token(settings) == TOKEN
    settings.xiaoyuzhou_token_file.write_text("new-token", encoding="utf-8")
    assert platform.read_access_token(settings) == "new-token"
    settings.xiaoyuzhou_access_token = TOKEN
    assert TOKEN not in repr(settings)
    settings.xiaoyuzhou_access_token = "bad\nheader"
    assert platform.credentials_configured(settings) is False


def test_platform_workflow_skips_audio_model_and_reuses_text_only_result(settings, monkeypatch, episode):
    settings.transcript_mode = "platform"
    monkeypatch.setattr("app.main.check_access_token", lambda *args: {"status": "valid", "message": "验证通过"})
    app = create_app(settings, worker_enabled=False)
    monkeypatch.setattr("app.worker.fetch_episode", lambda url: episode)
    result = platform.PlatformTranscript([Segment(0, 1, "平台原文")], True)
    monkeypatch.setattr("app.worker.fetch_platform_transcript", lambda *args: result)

    def unexpected(*args):
        pytest.fail("Existing platform transcript must not download audio or load ASR")

    monkeypatch.setattr("app.worker.download_audio", unexpected)
    monkeypatch.setattr(app.state.worker.transcriber, "load_model", unexpected)
    with TestClient(app) as client:
        created = client.post("/api/jobs", json={"url": URL}).json()
        app.state.worker.process(app.state.store.claim())
        job = client.get(f"/api/jobs/{created['job_id']}").json()
        assert job["status"] == "COMPLETED"
        assert job["transcript_source"] == "xiaoyuzhou"
        assert set(job["files"]) == {"txt", "md", "srt", "vtt", "json"}
        data = client.get(f"/api/jobs/{created['job_id']}/transcript").json()
        assert data["model"] is None
        assert data["language"] == "und"
        assert data["end_timestamps_inferred"] is True
        assert "小宇宙平台已有文稿" in client.get(job["files"]["md"]).content.decode("utf-8")
        assert client.post("/api/jobs", json={"url": URL}).json()["status"] == "COMPLETED"
        local = client.post("/api/jobs", json={"url": URL, "transcript_mode": "local"}).json()
        assert local["job_id"] != created["job_id"]
        with_audio = client.post("/api/jobs", json={"url": URL, "download_audio": True}).json()
        assert with_audio["job_id"] != created["job_id"]


@pytest.mark.parametrize("code", ["PLATFORM_TRANSCRIPT_MISSING", "PLATFORM_AUTH_REQUIRED", "PLATFORM_AUTH_EXPIRED"])
def test_legacy_auto_job_no_longer_falls_back(settings, store, monkeypatch, episode, wav_bytes, code):
    from test_workflow import install_fake_dependencies

    job = store.enqueue(URL, EPISODE_ID, "zh", "tiny", transcript_mode="auto", download_audio=False)[0]
    worker = Worker(settings, store)
    install_fake_dependencies(monkeypatch, episode, wav_bytes, worker)

    def unavailable(*args):
        raise JobError(code, "平台文稿不可用")

    monkeypatch.setattr("app.worker.fetch_platform_transcript", unavailable)
    worker.process(store.claim())
    result = store.get(job["id"])
    assert result["status"] == "FAILED"
    assert result["error_code"] == code
    assert result["transcript_source"] is None
    assert result["files_json"] is None


def test_platform_only_never_falls_back(settings, store, monkeypatch, episode):
    job = store.enqueue(URL, EPISODE_ID, "zh", "tiny", transcript_mode="platform")[0]
    monkeypatch.setattr("app.worker.fetch_episode", lambda url: episode)

    def unavailable(*args):
        raise JobError("PLATFORM_AUTH_REQUIRED", "请配置凭据")

    monkeypatch.setattr("app.worker.fetch_platform_transcript", unavailable)
    worker = Worker(settings, store)
    monkeypatch.setattr(worker.transcriber, "load_model", lambda *args: pytest.fail("No fallback"))
    worker.process(store.claim())
    result = store.get(job["id"])
    assert result["status"] == "FAILED"
    assert result["error_code"] == "PLATFORM_AUTH_REQUIRED"


def test_text_is_published_before_optional_audio_and_survives_audio_failure(settings, store, monkeypatch, episode):
    job = store.enqueue(URL, EPISODE_ID, "zh", "tiny", transcript_mode="auto", download_audio=True)[0]
    monkeypatch.setattr("app.worker.fetch_episode", lambda url: episode)
    monkeypatch.setattr(
        "app.worker.fetch_platform_transcript", lambda *args: platform.PlatformTranscript([Segment(0, 1, "文稿")], True)
    )

    def download(*args):
        running = store.get(job["id"])
        assert "json" in running["files_json"]
        assert running["status"] == "RUNNING"
        raise JobError("AUDIO_DOWNLOAD_FAILED", "音频下载失败，文稿已保存")

    monkeypatch.setattr("app.worker.download_audio", download)
    Worker(settings, store).process(store.claim())
    result = store.get(job["id"])
    assert result["status"] == "FAILED"
    assert "json" in result["files_json"]
    assert result["transcript_source"] == "xiaoyuzhou"


def test_stop_during_platform_fetch_requeues_without_fallback(settings, store, monkeypatch, episode):
    job = store.enqueue(URL, EPISODE_ID, "zh", "tiny", transcript_mode="auto")[0]
    monkeypatch.setattr("app.worker.fetch_episode", lambda url: episode)

    def stop(*args):
        raise JobInterrupted()

    monkeypatch.setattr("app.worker.fetch_platform_transcript", stop)
    Worker(settings, store).process(store.claim())
    assert store.get(job["id"])["status"] == "QUEUED"


def test_health_never_exposes_token(settings):
    settings.xiaoyuzhou_access_token = TOKEN
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        response = client.get("/api/health")
        assert response.json()["platform_credentials_configured"] is True
        assert TOKEN not in response.text


@pytest.mark.parametrize(("http_status", "body", "expected"), [
    (200, {"data": {"transcriptUrl": CDN}}, "valid"),
    (200, {"data": {}}, "no_transcript"),
    (401, {"secret": TOKEN}, "rejected"),
    (403, {"secret": TOKEN}, "forbidden"),
    (404, {}, "no_transcript"),
    (500, {"secret": TOKEN}, "unknown"),
    (302, {}, "unknown"),
    (200, ["unexpected-payload"], "no_transcript"),
])
def test_token_preflight_uses_read_endpoint_without_creating_job(
    settings, episode, monkeypatch, http_status, body, expected,
):
    ready(settings, episode)
    monkeypatch.setattr("app.xiaoyuzhou.fetch_episode", lambda url: episode)
    requests = []

    def handle(request):
        requests.append(request)
        assert str(request.url) == platform.API_URL
        assert request.headers["x-jike-access-token"] == TOKEN
        return httpx.Response(http_status, json=body, headers={"location": "https://evil.test"})

    mock_http(monkeypatch, handle)
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        result = client.post("/api/platform/check-token", json={"url": URL})
        assert result.status_code == 200
        assert result.json()["status"] == expected
        assert TOKEN not in result.text
        assert CDN not in result.text
        assert result.headers["cache-control"] == "no-store"
        assert client.get("/api/jobs").json() == []
    assert len(requests) == 1  # Never downloads the transcript or any audio.


def test_token_preflight_reloads_credentials_without_restart(settings, episode, monkeypatch):
    episode.transcript_media_id = "podcast-id/audio.m4a"
    monkeypatch.setattr("app.xiaoyuzhou.fetch_episode", lambda url: episode)
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["x-jike-access-token"] == "updated-token"
        return httpx.Response(200, json={"data": {"transcriptUrl": CDN}})

    mock_http(monkeypatch, handle)
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        assert client.post("/api/platform/check-token", json={"url": URL}).json()["status"] == "missing"
        assert not requests
        settings.xiaoyuzhou_token_file.write_text("updated-token", encoding="utf-8")
        assert client.post("/api/platform/check-token", json={}).json()["status"] == "unchecked"
        assert not requests
        assert client.post("/api/platform/check-token", json={"url": URL}).json()["status"] == "valid"
        assert len(requests) == 1


def test_token_preflight_network_error_is_unknown_and_sanitized(settings, episode, monkeypatch):
    ready(settings, episode)
    monkeypatch.setattr("app.xiaoyuzhou.fetch_episode", lambda url: episode)

    def failure(request):
        raise httpx.ReadTimeout(TOKEN + CDN)

    mock_http(monkeypatch, failure)
    result = platform.check_access_token(settings, URL)
    assert result["status"] == "unknown"
    assert TOKEN not in str(result)
    assert CDN not in str(result)


def test_token_preflight_rejects_cross_site_and_unsafe_input(settings, monkeypatch):
    def unexpected(*args):
        pytest.fail("Invalid preflight must not send network requests")

    monkeypatch.setattr("app.xiaoyuzhou.fetch_episode", unexpected)
    with TestClient(create_app(settings, worker_enabled=False)) as client:
        assert client.post("/api/platform/check-token", json={"url": "https://evil.test"}).status_code == 400
        assert client.post(
            "/api/platform/check-token", json={"url": URL}, headers={"Origin": "https://evil.test"}
        ).status_code == 403


def test_platform_success_with_audio_still_skips_asr(settings, store, monkeypatch, episode, wav_bytes):
    from test_workflow import install_fake_dependencies

    job = store.enqueue(URL, EPISODE_ID, "zh", "tiny", transcript_mode="auto", download_audio=True)[0]
    worker = Worker(settings, store)
    install_fake_dependencies(monkeypatch, episode, wav_bytes, worker)
    monkeypatch.setattr(
        "app.worker.fetch_platform_transcript", lambda *args: platform.PlatformTranscript([Segment(0, 1, "原文")], True)
    )
    monkeypatch.setattr(worker.transcriber, "load_model", lambda *args: pytest.fail("No ASR needed"))
    worker.process(store.claim())
    result = store.get(job["id"])
    assert result["status"] == "COMPLETED"
    assert set(result["files_json"]) == {"audio", "txt", "md", "srt", "vtt", "json"}
    assert result["transcript_source"] == "xiaoyuzhou"


def test_cdn_private_dns_is_rejected(settings, episode, monkeypatch):
    ready(settings, episode)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"transcriptUrl": CDN}})

    mock_http(monkeypatch, handle)
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(JobError) as error:
        platform.fetch_platform_transcript(episode, settings, lambda: None)
    assert error.value.code == "PLATFORM_TRANSCRIPT_UNSAFE_URL"
    assert len(requests) == 1
