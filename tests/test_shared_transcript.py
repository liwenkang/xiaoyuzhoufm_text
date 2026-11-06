from types import SimpleNamespace

import pytest

from app import shared_transcript as shared
from app.credentials import CredentialManager, Credentials
from app.errors import AppError
from app.platform_transcript import Segment
from app.state import SQLiteStore


@pytest.fixture
def setup(tmp_path, monkeypatch, episode):
    store = SQLiteStore(tmp_path / "state.sqlite3")
    CredentialManager(store).import_credentials("private-access", "private-refresh")
    service = shared.SharedTranscriptService(store)
    calls = []

    def fetch(_episode, token):
        calls.append(token)
        return SimpleNamespace(segments=[Segment(0, 12, "你好")], end_timestamps_inferred=True)

    monkeypatch.setattr(shared, "fetch_episode", lambda _url: episode)
    monkeypatch.setattr(shared, "fetch_platform_transcript", fetch)
    return service, store, calls


def test_success_is_shared_cached_and_contains_no_credentials(setup, episode):
    service, store, calls = setup
    first = service.fetch(episode.source_url, episode.episode_id)
    other = shared.SharedTranscriptService(store)
    assert other.fetch(episode.source_url, episode.episode_id) == first
    assert calls == ["private-access"]
    assert first["segments"] == [{"start": 0, "end": 12, "text": "你好", "speaker": None}]
    assert first["end_timestamps_inferred"] is True
    for secret in ("private-access", "private-refresh", "audio_url", "transcript_media_id", "shownotes"):
        assert secret not in str(first)
    assert store.get("upstream-lock") is None


def test_401_refreshes_once_and_retries_with_new_token(setup, monkeypatch, episode):
    service, store, calls = setup
    fetch = shared.fetch_platform_transcript

    def expired(_episode, token):
        if token == "private-access":
            calls.append(token)
            raise AppError("PLATFORM_AUTH_EXPIRED", "expired")
        return fetch(_episode, token)

    monkeypatch.setattr(shared, "fetch_platform_transcript", expired)
    monkeypatch.setattr("app.credentials.refresh_credentials", lambda _c: Credentials("new-access", "rotated"))
    service.fetch(episode.source_url, episode.episode_id)
    assert calls == ["private-access", "new-access"]
    assert CredentialManager(store).read().refresh_token == "rotated"


@pytest.mark.parametrize("code", ["PLATFORM_AUTH_FORBIDDEN", "PLATFORM_TRANSCRIPT_MISSING"])
def test_permission_or_missing_transcript_never_refreshes_or_caches(setup, monkeypatch, episode, code):
    service, store, _calls = setup

    def fail(*_args):
        raise AppError(code, "不可用")

    monkeypatch.setattr(shared, "fetch_platform_transcript", fail)
    monkeypatch.setattr("app.credentials.refresh_credentials", lambda _c: pytest.fail("Unexpected refresh"))
    with pytest.raises(AppError) as error:
        service.fetch(episode.source_url, episode.episode_id)
    assert error.value.code == code
    assert store.get(f"transcript:{episode.episode_id}") is None
    assert store.get("upstream-lock") is None


def test_a_second_401_never_creates_an_infinite_retry_loop(setup, monkeypatch, episode):
    service, store, calls = setup

    def fail(_episode, token):
        calls.append(token)
        raise AppError("PLATFORM_AUTH_EXPIRED", "expired")

    monkeypatch.setattr(shared, "fetch_platform_transcript", fail)
    monkeypatch.setattr("app.credentials.refresh_credentials", lambda _c: Credentials("new-access", "rotated"))
    with pytest.raises(AppError) as error:
        service.fetch(episode.source_url, episode.episode_id)
    assert error.value.code == "SHARED_AUTH_REFRESH_FAILED"
    assert calls == ["private-access", "new-access"]
    assert store.get(f"transcript:{episode.episode_id}") is None
    with pytest.raises(AppError) as error:
        service.fetch(episode.source_url, episode.episode_id)
    assert error.value.code == "SHARED_AUTH_REFRESH_FAILED"
    assert calls == ["private-access", "new-access"]


def test_global_limit_is_shared_across_instances_and_includes_cache_hits(setup, monkeypatch, episode):
    service, store, calls = setup
    monkeypatch.setattr(shared, "RATE_LIMIT", 2)
    service.fetch(episode.source_url, episode.episode_id)
    other = shared.SharedTranscriptService(store)
    other.fetch(episode.source_url, episode.episode_id)
    with pytest.raises(AppError) as error:
        other.fetch(episode.source_url, episode.episode_id)
    assert error.value.code == "SHARED_RATE_LIMITED"
    assert calls == ["private-access"]


def test_cache_miss_budget_limits_upstream_work(setup, monkeypatch, episode):
    service, _store, calls = setup
    monkeypatch.setattr(shared, "MISS_LIMIT", 1)
    service.fetch(episode.source_url, episode.episode_id)
    with pytest.raises(AppError) as error:
        service.fetch(episode.source_url, "b" * 24)
    assert error.value.code == "SHARED_RATE_LIMITED"
    assert calls == ["private-access"]


def test_overlapping_cold_requests_do_not_hit_upstream(setup, episode):
    service, store, calls = setup
    assert store.claim("upstream-lock", "other-instance", 240)
    with pytest.raises(AppError) as error:
        service.fetch(episode.source_url, episode.episode_id)
    assert error.value.code == "SHARED_SERVICE_BUSY"
    assert calls == []


def test_existing_cache_survives_login_recovery_status(setup, episode):
    service, store, calls = setup
    first = service.fetch(episode.source_url, episode.episode_id)
    store.put("credentials", store.get("credentials") | {"recovery_required": True})
    assert service.fetch(episode.source_url, episode.episode_id) == first
    assert calls == ["private-access"]
