"""Shared state with expiring leases; Redis in production, SQLite locally."""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .errors import AppError

PREFIX = "xyztext:{v1}:"


def unavailable():
    return AppError("SHARED_STORE_UNAVAILABLE", "服务凭据存储暂不可用，请稍后重试或联系维护者。")


class SQLiteStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def transaction(self):
        connection = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=3)
            connection.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT, expires REAL)")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM state WHERE expires IS NOT NULL AND expires <= ?", (time.time(),))
            yield connection
            connection.commit()
        except (OSError, sqlite3.Error, ValueError, TypeError):
            if connection:
                connection.rollback()
            raise unavailable() from None
        finally:
            if connection:
                connection.close()

    def get(self, key):
        with self.transaction() as connection:
            row = connection.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def put(self, key, value, ttl=None):
        with self.transaction() as connection:
            connection.execute("INSERT OR REPLACE INTO state VALUES (?, ?, ?)",
                               (key, json.dumps(value), time.time() + ttl if ttl else None))

    def claim(self, key, owner, ttl):
        with self.transaction() as connection:
            cursor = connection.execute("INSERT OR IGNORE INTO state VALUES (?, ?, ?)",
                                        (key, json.dumps(owner), time.time() + ttl))
            return cursor.rowcount == 1

    def release(self, key, owner):
        with self.transaction() as connection:
            connection.execute("DELETE FROM state WHERE key = ? AND value = ?", (key, json.dumps(owner)))

    def put_locked(self, key, value, lock_key, owner):
        with self.transaction() as connection:
            row = connection.execute("SELECT value FROM state WHERE key = ?", (lock_key,)).fetchone()
            if not row or json.loads(row[0]) != owner:
                raise unavailable()
            connection.execute("INSERT OR REPLACE INTO state VALUES (?, ?, NULL)", (key, json.dumps(value)))

    def increment(self, key, ttl):
        with self.transaction() as connection:
            row = connection.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
            count = int(json.loads(row[0])) + 1 if row else 1
            if row:
                connection.execute("UPDATE state SET value = ? WHERE key = ?", (json.dumps(count), key))
            else:
                connection.execute("INSERT INTO state VALUES (?, ?, ?)", (key, json.dumps(count), time.time() + ttl))
            return count


class RedisStore:
    def __init__(self, url):
        try:
            from redis import Redis
            self.client = Redis.from_url(url, decode_responses=True, socket_timeout=3, socket_connect_timeout=3)
        except (ImportError, ValueError):
            raise unavailable() from None

    def command(self, name, *args, **kwargs):
        from redis.exceptions import RedisError
        try:
            return getattr(self.client, name)(*args, **kwargs)
        except (RedisError, OSError, ValueError, TypeError):
            raise unavailable() from None

    def get(self, key):
        value = self.command("get", PREFIX + key)
        try:
            return json.loads(value) if value is not None else None
        except (ValueError, TypeError):
            raise unavailable() from None

    def put(self, key, value, ttl=None):
        self.command("set", PREFIX + key, json.dumps(value), ex=ttl)

    def claim(self, key, owner, ttl):
        return bool(self.command("set", PREFIX + key, json.dumps(owner), nx=True, ex=ttl))

    def release(self, key, owner):
        self.command("eval", "if redis.call('get', KEYS[1]) == ARGV[1] then "
                     "return redis.call('del', KEYS[1]) else return 0 end", 1, PREFIX + key, json.dumps(owner))

    def put_locked(self, key, value, lock_key, owner):
        saved = self.command("eval", "if redis.call('get', KEYS[1]) == ARGV[1] then "
                             "redis.call('set', KEYS[2], ARGV[2]); return 1 else return 0 end",
                             2, PREFIX + lock_key, PREFIX + key, json.dumps(owner), json.dumps(value))
        if not saved:
            raise unavailable()

    def increment(self, key, ttl):
        return int(self.command("eval", "local n = redis.call('incr', KEYS[1]); "
                                "if n == 1 then redis.call('expire', KEYS[1], ARGV[1]) end; return n",
                                1, PREFIX + key, ttl))


class UnconfiguredStore:
    def get(self, *_args, **_kwargs):
        raise AppError("SHARED_AUTH_UNCONFIGURED", "服务尚未配置共享凭据存储，请联系维护者。")

    increment = get
    claim = get


def create_store():
    url = os.environ.get("REDIS_URL", "").strip()
    if url:
        return RedisStore(url)
    if os.environ.get("VERCEL"):
        # Never place rotating credentials on an ephemeral Function filesystem.
        return UnconfiguredStore()
    return SQLiteStore(Path(__file__).resolve().parent.parent / ".secrets" / "state.sqlite3")


@contextmanager
def lease(store, key, ttl=60):
    owner = uuid4().hex
    if not store.claim(key, owner, ttl):
        raise AppError("SHARED_SERVICE_BUSY", "服务正在处理其他请求，请稍后重试。")
    try:
        yield owner
    finally:
        store.release(key, owner)
