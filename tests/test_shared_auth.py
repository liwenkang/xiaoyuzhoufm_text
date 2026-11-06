import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from threading import Event

import fakeredis
import httpx
import pytest

from app import credentials as auth
from app.credentials import CREDENTIAL_KEY, LOCK_KEY, CredentialManager, Credentials
from app.errors import AppError
from app.state import PREFIX, RedisStore, SQLiteStore, create_store, lease

HTTP_CLIENT = httpx.Client


@pytest.fixture(params=["sqlite", "redis"])
def store(request, tmp_path):
    if request.param == "sqlite":
        return SQLiteStore(tmp_path / "state.sqlite3")
    result = RedisStore("redis://localhost:6379")
    result.client = fakeredis.FakeRedis(decode_responses=True)
    return result


def seed(store, access="old-access", refresh="old-refresh", expires_at=None):
    store.put(CREDENTIAL_KEY, asdict(Credentials(access, refresh, expires_at)))
    return CredentialManager(store)


def jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def mock_refresh(monkeypatch, handler):
    monkeypatch.setattr(auth.httpx, "Client",
                        lambda **kw: HTTP_CLIENT(transport=httpx.MockTransport(handler), **kw))


def test_fresh_credentials_reused_without_network(store, monkeypatch):
    manager = seed(store, expires_at=time.time() + 3600)
    monkeypatch.setattr(auth, "refresh_credentials", lambda _current: pytest.fail("Unnecessary refresh"))
    assert manager.access() == "old-access"
    assert "old-access" not in repr(manager.read())


@pytest.mark.parametrize("access", ["", jwt(1)])
def test_refresh_persists_rotating_pair_before_returning(store, monkeypatch, access):
    manager = seed(store, access=access, expires_at=auth.token_expiry(access))
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == auth.REFRESH_URL
        assert request.headers["x-jike-refresh-token"] == "old-refresh"
        assert "x-jike-access-token" not in request.headers
        return httpx.Response(200, headers={"x-jike-access-token": "new-access",
                                           "x-jike-refresh-token": "new-refresh"})

    mock_refresh(monkeypatch, handle)
    assert manager.access() == "new-access"
    assert manager.read().refresh_token == "new-refresh"
    assert manager.access() == "new-access"
    assert len(requests) == 1
    assert store.get(LOCK_KEY) is None


def test_refresh_without_rotation_keeps_existing_refresh(store, monkeypatch):
    manager = seed(store)
    mock_refresh(monkeypatch, lambda _r: httpx.Response(200, headers={"x-jike-access-token": "new"}))
    assert manager.access(rejected="old-access") == "new"
    assert manager.read().refresh_token == "old-refresh"


def test_partial_rotation_is_saved_even_without_access(store, monkeypatch):
    manager = seed(store)
    mock_refresh(monkeypatch, lambda _r: httpx.Response(200, headers={"x-jike-refresh-token": "rotated"}))
    with pytest.raises(AppError, match="响应异常"):
        manager.access(rejected="old-access")
    assert manager.read().refresh_token == "rotated"
    assert manager.read().access_token == ""
    assert manager.read().retry_after > time.time()


@pytest.mark.parametrize("status", [401, 403, 500, 429, 302, 200])
def test_failed_refresh_is_sanitized_and_cools_down(store, monkeypatch, status):
    manager = seed(store, access="")
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, text="old-refresh", headers={"location": "https://evil.test/steal"})

    mock_refresh(monkeypatch, handle)
    for _attempt in range(2):
        with pytest.raises(AppError) as error:
            manager.access()
        assert "old-refresh" not in str(error.value)
    assert len(requests) == 1
    assert manager.read().recovery_required is (status in (401, 403))
    assert store.get(LOCK_KEY) is None


def test_network_failure_does_not_echo_secret(store, monkeypatch):
    manager = seed(store, access="")

    def fail(request):
        raise httpx.ReadTimeout("old-refresh", request=request)

    mock_refresh(monkeypatch, fail)
    with pytest.raises(AppError) as error:
        manager.access()
    assert "old-refresh" not in str(error.value)
    assert manager.read().retry_after > time.time()


def test_two_managers_share_refresh_lock_and_reuse_rotated_pair(store, monkeypatch):
    manager = seed(store)
    other = CredentialManager(store)
    entered, release = Event(), Event()
    calls = []

    def refresh(current):
        calls.append(current.refresh_token)
        entered.set()
        assert release.wait(3)
        return Credentials("new-access", "new-refresh")

    monkeypatch.setattr(auth, "refresh_credentials", refresh)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(manager.access, "old-access")
        try:
            assert entered.wait(3)
            with pytest.raises(AppError) as error:
                other.access(rejected="old-access")
            assert error.value.code == "SHARED_SERVICE_BUSY"
        finally:
            release.set()
        assert future.result(timeout=3) == "new-access"
    assert other.access(rejected="old-access") == "new-access"
    assert calls == ["old-refresh"]


def test_expired_owner_cannot_delete_new_lock_or_overwrite_credentials(store):
    assert store.claim(LOCK_KEY, "old-owner", 60)
    store.release(LOCK_KEY, "old-owner")
    assert store.claim(LOCK_KEY, "new-owner", 60)
    store.release(LOCK_KEY, "old-owner")
    assert store.get(LOCK_KEY) == "new-owner"
    with pytest.raises(AppError):
        store.put_locked(CREDENTIAL_KEY, {"bad": True}, LOCK_KEY, "old-owner")
    assert store.get(CREDENTIAL_KEY) is None


def test_import_recovers_blocked_credentials_and_validates_input(store):
    manager = seed(store)
    store.put(CREDENTIAL_KEY, asdict(manager.read()) | {"recovery_required": True,
                                                       "retry_after": time.time() + 3600})
    manager.import_credentials("replacement", "replacement-refresh")
    assert manager.access() == "replacement"
    assert manager.read().recovery_required is False
    with pytest.raises(AppError):
        manager.import_credentials("bad\nheader", "replacement-refresh")
    assert manager.access() == "replacement"


def test_missing_and_malformed_state_fail_closed(store):
    manager = CredentialManager(store)
    with pytest.raises(AppError) as error:
        manager.access()
    assert error.value.code == "SHARED_AUTH_UNCONFIGURED"
    store.put(CREDENTIAL_KEY, {"refresh_token": "secret"})
    with pytest.raises(AppError) as error:
        manager.access()
    assert error.value.code == "SHARED_STORE_UNAVAILABLE"
    assert "secret" not in str(error.value)


def test_sqlite_credentials_survive_new_store_instance(tmp_path):
    path = tmp_path / "state.sqlite3"
    seed(SQLiteStore(path)).import_credentials("persisted-access", "persisted-refresh")
    assert CredentialManager(SQLiteStore(path)).access() == "persisted-access"


def test_sqlite_expiry_cleans_values_and_leases(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "state.sqlite3")
    now = time.time()
    monkeypatch.setattr("app.state.time.time", lambda: now)
    store.put("cache", {"segments": []}, ttl=10)
    assert store.claim("lock", "old", 10)
    monkeypatch.setattr("app.state.time.time", lambda: now + 11)
    assert store.get("cache") is None
    assert store.claim("lock", "new", 10)


def test_ttl_and_atomic_counter(store):
    assert store.increment("limit", 60) == 1
    assert store.increment("limit", 60) == 2
    store.put("cached", {"x": 1}, ttl=60)
    if isinstance(store, RedisStore):
        assert 0 < store.client.ttl(PREFIX + "limit") <= 60
        assert 0 < store.client.ttl(PREFIX + "cached") <= 60


def test_storage_failure_prevents_using_unpersisted_rotation(store, monkeypatch):
    manager = seed(store, access="")
    monkeypatch.setattr(auth, "refresh_credentials", lambda _c: Credentials("unpersisted", "rotated"))

    def fail(*_args):
        raise AppError("SHARED_STORE_UNAVAILABLE", "存储不可用")

    monkeypatch.setattr(store, "put_locked", fail)
    with pytest.raises(AppError) as error:
        manager.access()
    assert error.value.code == "SHARED_STORE_UNAVAILABLE"
    assert "unpersisted" not in str(error.value)


@pytest.mark.parametrize("value", ["opaque", "a.bad.b", jwt(True), jwt("123"), jwt(float("nan"))])
def test_untrusted_or_absent_expiry_is_only_a_hint(value):
    assert auth.token_expiry(value) is None


def test_expiry_hint():
    assert auth.token_expiry(jwt(1234)) == 1234


def test_redis_connection_failure_is_sanitized(monkeypatch):
    from redis.exceptions import ConnectionError
    store = RedisStore("redis://localhost:6379")

    def fail(*_args):
        raise ConnectionError("redis://secret-password@host")

    monkeypatch.setattr(store.client, "get", fail)
    with pytest.raises(AppError) as error:
        store.get("credentials")
    assert "secret-password" not in str(error.value)


def test_vercel_never_uses_local_sqlite(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(AppError):
        create_store().get(CREDENTIAL_KEY)


def test_lease_is_released_on_failure(store):
    with pytest.raises(RuntimeError):
        with lease(store, "test-lock"):
            raise RuntimeError
    assert store.get("test-lock") is None
