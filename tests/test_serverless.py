from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.errors import AppError
from app.serverless import create_serverless_app
from app.shared_transcript import SharedTranscriptService
from app.state import SQLiteStore


@pytest.fixture
def service(tmp_path):
    return SharedTranscriptService(SQLiteStore(tmp_path / "state.sqlite3"))


def test_transcript_endpoint_accepts_only_url(service):
    calls = []
    service.fetch = lambda url, eid: calls.append((url, eid)) or {"segments": []}
    with TestClient(create_serverless_app(service)) as client:
        response = client.post("/api/transcript", json={"url": "a" * 24})
    assert response.status_code == 200
    assert response.json() == {"segments": []}
    assert calls == [("https://www.xiaoyuzhoufm.com/episode/" + "a" * 24, "a" * 24)]


def test_transcript_endpoint_rejects_cross_origin_post(service):
    with TestClient(create_serverless_app(service)) as client:
        response = client.post("/api/transcript", json={"url": "a" * 24},
                               headers={"Origin": "https://evil.example", "Host": "testserver"})
    assert response.status_code == 403


@pytest.mark.parametrize("payload", [{}, {"url": None}, {"url": []},
                                    {"url": "a" * 24, "token": "do-not-echo"}])
def test_invalid_or_old_payload_is_rejected_without_credential_echo(service, payload):
    with TestClient(create_serverless_app(service)) as client:
        response = client.post("/api/transcript", json=payload)
    assert response.status_code == 422
    assert "do-not-echo" not in response.text
    assert "token" not in response.text


def test_missing_credentials_explain_service_recovery(service):
    with TestClient(create_serverless_app(service)) as client:
        response = client.post("/api/transcript", json={"url": "a" * 24})
    assert response.status_code == 503
    assert response.json()["error_code"] == "SHARED_AUTH_UNCONFIGURED"
    assert "维护者" in response.json()["detail"]


def test_public_files_and_health(service):
    with TestClient(create_serverless_app(service)) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "无需登录或填写账号凭据" in home.text
        assert "access-token" not in home.text
        for name in ("app.js", "style.css"):
            assert client.get(f"/static/{name}").status_code == 200
        health = client.get("/api/health")
        assert health.json() == {
            "status": "ok", "transcript_mode": "platform", "auth_mode": "server_shared",
        }
        assert health.headers["cache-control"] == "no-store"
        for path in ("/api/jobs", "/api/platform/token", "/api/credentials", "/.secrets/state.sqlite3"):
            assert client.get(path).status_code == 404


def test_api_runs_without_public_directory(monkeypatch, tmp_path, service):
    monkeypatch.setattr("app.serverless.PUBLIC_DIR", tmp_path / "missing-public")
    with TestClient(create_serverless_app(service)) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.post("/api/transcript", json={"url": "a" * 24}).status_code == 503


@pytest.mark.parametrize(("code", "status"), [
    ("PLATFORM_AUTH_FORBIDDEN", 403), ("PLATFORM_TRANSCRIPT_MISSING", 400),
    ("SHARED_AUTH_REFRESH_FAILED", 503), ("SHARED_AUTH_RECOVERY_REQUIRED", 503),
    ("SHARED_STORE_UNAVAILABLE", 503), ("SHARED_SERVICE_BUSY", 503), ("SHARED_RATE_LIMITED", 429),
])
def test_errors_keep_status_without_cache_or_secrets(code, status):
    def fail(*_args):
        raise AppError(code, "请稍后重试或联系维护者。")

    with TestClient(create_serverless_app(SimpleNamespace(fetch=fail))) as client:
        response = client.post("/api/transcript", json={"url": "a" * 24})
    assert response.status_code == status
    assert response.json()["error_code"] == code
    assert response.headers["cache-control"] == "no-store"
    if status == 429:
        assert response.headers["retry-after"] == "60"


def test_vercel_without_redis_fails_closed(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("REDIS_URL", raising=False)
    with TestClient(create_serverless_app()) as client:
        response = client.post("/api/transcript", json={"url": "a" * 24})
    assert response.status_code == 503
    assert response.json()["error_code"] == "SHARED_AUTH_UNCONFIGURED"
