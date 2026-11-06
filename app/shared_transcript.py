"""Bound shared-account traffic and reuse successful public transcripts."""

import json
import time
from dataclasses import asdict

from .credentials import CredentialManager
from .errors import AppError
from .platform_transcript import fetch_platform_transcript
from .state import create_store, lease
from .xiaoyuzhou import fetch_episode

RATE_LIMIT = 30
MISS_LIMIT = 10
CACHE_TTL = 3600


class SharedTranscriptService:
    def __init__(self, store=None):
        self.store = store if store is not None else create_store()
        self.credentials = CredentialManager(self.store)

    def fetch(self, url, episode_id):
        # A single global budget works across Function instances and cannot be
        # bypassed by forging client IP headers. Cache hits count toward it too.
        minute = int(time.time() // 60)
        if self.store.increment(f"requests:{minute}", 120) > RATE_LIMIT:
            raise AppError("SHARED_RATE_LIMITED", "服务请求较多，请一分钟后重试。")
        cache_key = f"transcript:{episode_id}"
        cached = self.store.get(cache_key)
        if cached is not None:
            return cached
        if self.store.increment(f"misses:{minute}", 120) > MISS_LIMIT:
            raise AppError("SHARED_RATE_LIMITED", "服务读取新节目的请求较多，请一分钟后重试。")
        # Globally serialize upstream work: bound account pressure and prevent
        # concurrent cold requests from generating a credential refresh storm.
        with lease(self.store, "upstream-lock", ttl=240):
            cached = self.store.get(cache_key)
            if cached is not None:
                return cached
            token = self.credentials.access()
            episode = fetch_episode(url)
            try:
                result = fetch_platform_transcript(episode, token)
            except AppError as error:
                if error.code != "PLATFORM_AUTH_EXPIRED":
                    raise
                token = self.credentials.access(rejected=token)
                try:
                    result = fetch_platform_transcript(episode, token)
                except AppError as retry_error:
                    if retry_error.code == "PLATFORM_AUTH_EXPIRED":
                        self.credentials.defer_rejected_access(token)
                        raise AppError("SHARED_AUTH_REFRESH_FAILED", "服务登录校验失败，请联系维护者。") from None
                    raise
            episode_data = episode.as_dict()
            for key in ("audio_url", "shownotes", "transcript_media_id"):
                episode_data.pop(key, None)
            body = {
                "episode": episode_data,
                "transcript_source": "xiaoyuzhou",
                "source_note": "文字来自小宇宙平台已有文稿，未在本机重新识别。",
                "end_timestamps_inferred": result.end_timestamps_inferred,
                "segments": [asdict(segment) for segment in result.segments],
            }
            # Keep large exports out of Redis; a cache miss remains functional.
            if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) <= 1024 * 1024:
                self.store.put(cache_key, body, ttl=CACHE_TTL)
            return body
