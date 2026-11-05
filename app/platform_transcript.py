"""Read existing Xiaoyuzhou transcripts using the operator's own access token.

The endpoint is not a documented public API. Keep its protocol isolated here.
Credentials are sent only to the fixed API origin, never to transcript/CDN URLs.
"""

import json
import math
import os
import tempfile
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from .errors import JobError
from .media import validate_audio_url
from .transcriber import Segment

API_URL = "https://api.xiaoyuzhoufm.com/v1/episode-transcript/get"
APP_USER_AGENT = "Xiaoyuzhou/2.7.0 (build:1234; iOS 17.0.0)"
TRANSCRIPT_HOSTS = (".xyzcdn.net",)


@dataclass
class PlatformTranscript:
    segments: list[Segment]
    end_timestamps_inferred: bool


def validate_access_token(token):
    token = token.strip()
    if not token or len(token) > 16384 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise JobError("PLATFORM_AUTH_INVALID", "请填写有效的 access token，仅粘贴 token 值，不要包含请求头名称或空格。")
    return token


def save_access_token(settings, token):
    token = validate_access_token(token)
    if settings.xiaoyuzhou_access_token.strip():
        raise JobError(
            "PLATFORM_AUTH_ENV_OVERRIDE",
            "当前凭据由环境变量提供。请移除 XIAOYUZHOU_ACCESS_TOKEN 并重启服务后，再从页面保存。",
        )
    path = settings.xiaoyuzhou_token_file
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as output:
            temporary = output.name
            output.write(token)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError:
        raise JobError("PLATFORM_AUTH_SAVE_FAILED", "保存 access token 失败，请检查本机凭据文件的写入权限。") from None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def read_access_token(settings):
    token = settings.xiaoyuzhou_access_token.strip()
    if not token:
        try:
            if settings.xiaoyuzhou_token_file.stat().st_size > 16384:
                raise JobError("PLATFORM_AUTH_INVALID", "小宇宙登录凭据文件过大，请只保存 access token。")
            token = settings.xiaoyuzhou_token_file.read_text(encoding="utf-8-sig").strip()
        except FileNotFoundError:
            token = ""
        except (OSError, UnicodeError):
            raise JobError("PLATFORM_AUTH_INVALID", "无法读取小宇宙登录凭据文件，请检查本机配置。") from None
    if not token:
        raise JobError(
            "PLATFORM_AUTH_REQUIRED", "尚未配置小宇宙登录凭据，请按 README 配置自己的 access token。"
        )
    return validate_access_token(token)


def credentials_configured(settings):
    try:
        read_access_token(settings)
        return True
    except JobError:
        return False


def _read_json(response, limit, check_stop):
    chunks, size = [], 0
    for chunk in response.iter_bytes():
        check_stop()
        size += len(chunk)
        if size > limit:
            raise JobError("PLATFORM_TRANSCRIPT_INVALID", "平台文稿响应过大，已停止读取。")
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except (ValueError, UnicodeError, RecursionError):
        raise JobError("PLATFORM_TRANSCRIPT_INVALID", "平台文稿不是有效的 JSON 数据。") from None


def _milliseconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid timestamp")
    if not math.isfinite(value) or value < 0:
        raise ValueError("Invalid timestamp")
    return round(value / 1000, 3)


def parse_segments(payload, duration):
    """Convert milliseconds to seconds; infer missing ends from the next start.

Do not silently discard malformed speech segments or guess a timestamp unit.
"""
    try:
        if not isinstance(payload, list) or len(payload) > 200000:
            raise ValueError("Expected a segment array")
        rows = []
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise ValueError("Invalid segment")
            text = item["text"].strip()
            if not text:
                continue
            start = _milliseconds(item.get("startMs"))
            end = _milliseconds(item["endMs"]) if item.get("endMs") is not None else None
            if rows and start < rows[-1][0]:
                raise ValueError("Unordered timestamps")
            if duration and start >= duration:
                raise ValueError("Timestamp exceeds episode duration")
            rows.append((start, end, text))
        if not rows:
            raise ValueError("Empty transcript")
        inferred = False
        next_start = duration
        segments = []
        for index in range(len(rows) - 1, -1, -1):
            start, end, text = rows[index]
            if index + 1 < len(rows) and rows[index + 1][0] > start:
                next_start = rows[index + 1][0]
            if end is None:
                end = next_start
                inferred = True
            if end is None or end <= start or (duration and end > duration + 2):
                raise ValueError("Invalid segment end")
            segments.append(Segment(start, round(end, 3), text))
        return PlatformTranscript(list(reversed(segments)), inferred)
    except (ValueError, TypeError, OverflowError):
        raise JobError(
            "PLATFORM_TRANSCRIPT_INVALID", "平台文稿为空或时间戳格式不受支持，未将其作为完整文稿保存。"
        ) from None


def _fetch_transcript_url(episode, token, check_stop):
    check_stop()
    if not episode.transcript_media_id:
        raise JobError("PLATFORM_TRANSCRIPT_MISSING", "小宇宙公开页面未提供这期节目的文稿标识。")
    headers = {
        "User-Agent": APP_USER_AGENT,
        "x-jike-app-version": "2.7.0",
        "x-jike-device-id": "00000000-0000-0000-0000-000000000000",
        "x-jike-access-token": token,
    }
    try:
        # The authenticated client cannot follow redirects or be reused for the CDN.
        with httpx.Client(timeout=httpx.Timeout(20, connect=10), follow_redirects=False) as client:
            with client.stream(
                "POST", API_URL, headers=headers,
                json={"eid": episode.episode_id, "mediaId": episode.transcript_media_id},
            ) as response:
                if response.status_code == 401:
                    raise JobError(
                        "PLATFORM_AUTH_EXPIRED", "小宇宙登录凭据已失效或无效，请更新自己的 access token。"
                    )
                if response.status_code == 403:
                    raise JobError(
                        "PLATFORM_AUTH_FORBIDDEN", "小宇宙拒绝了文稿访问，可能没有权限；无法据此确认 token 是否过期。"
                    )
                if response.status_code == 404:
                    raise JobError("PLATFORM_TRANSCRIPT_MISSING", "小宇宙暂未提供这期的可用文稿。")
                if response.status_code != 200:
                    raise JobError("PLATFORM_TRANSCRIPT_UNAVAILABLE", "小宇宙文稿接口暂不可用，请稍后再试。")
                payload = _read_json(response, 256 * 1024, check_stop)
        data = payload.get("data") if isinstance(payload, dict) else None
        url = data.get("transcriptUrl") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url:
            raise JobError("PLATFORM_TRANSCRIPT_MISSING", "小宇宙暂未返回这期的可用文稿地址。")
        return url
    except httpx.HTTPError:
        raise JobError("PLATFORM_TRANSCRIPT_UNAVAILABLE", "读取平台文稿时网络连接失败，请稍后重试。") from None


def check_access_token(settings, url=None):
    """Probe the existing read endpoint without downloading media or creating a job.

    A transcript URL confirms access for this episode at this moment. A missing
    transcript or a network error cannot establish whether the token is valid.
    """
    try:
        token = read_access_token(settings)
    except JobError as exc:
        return {
            "status": "missing" if exc.code == "PLATFORM_AUTH_REQUIRED" else "invalid",
            "message": str(exc),
        }
    if not url:
        return {"status": "unchecked", "message": "已配置 access token；粘贴节目链接后自动验证是否可读取文稿。"}
    from .xiaoyuzhou import fetch_episode

    try:
        episode = fetch_episode(url)
        _fetch_transcript_url(episode, token, lambda: None)
        return {"status": "valid", "message": "access token 验证通过，当前可读取这期节目的平台文稿。"}
    except JobError as exc:
        if exc.code == "PLATFORM_AUTH_EXPIRED":
            return {"status": "rejected", "message": "access token 已失效或无效，请更新凭据后重新检查。"}
        if exc.code == "PLATFORM_AUTH_FORBIDDEN":
            return {"status": "forbidden", "message": str(exc)}
        if exc.code == "PLATFORM_TRANSCRIPT_MISSING":
            return {"status": "no_transcript", "message": "这期节目未提供可用的平台文稿，无法据此确认 token 是否有效。"}
        return {"status": "unknown", "message": "暂时无法验证 access token。" + str(exc)}


def fetch_platform_transcript(episode, settings, check_stop, access_token=None):
    check_stop()
    # Preserve the missing-transcript error even when no credentials are configured.
    if not episode.transcript_media_id:
        raise JobError("PLATFORM_TRANSCRIPT_MISSING", "小宇宙公开页面未提供这期节目的文稿标识。")
    token = validate_access_token(access_token) if access_token is not None else read_access_token(settings)
    url = _fetch_transcript_url(episode, token, check_stop)
    try:
        # A new client also avoids carrying API cookies to the CDN.
        with httpx.Client(
            timeout=httpx.Timeout(20, connect=10), follow_redirects=False,
            headers={"User-Agent": APP_USER_AGENT},
        ) as client:
            for _ in range(4):
                check_stop()
                try:
                    validate_audio_url(url, TRANSCRIPT_HOSTS)
                except JobError:
                    raise JobError(
                        "PLATFORM_TRANSCRIPT_UNSAFE_URL", "平台返回的文稿地址不在可信 HTTPS 域名范围内。"
                    ) from None
                with client.stream("GET", url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            break
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        raise JobError("PLATFORM_TRANSCRIPT_UNAVAILABLE", "平台文稿文件暂不可读取。")
                    return parse_segments(
                        _read_json(response, settings.max_transcript_bytes, check_stop), episode.duration
                    )
        raise JobError("PLATFORM_TRANSCRIPT_UNAVAILABLE", "平台文稿文件重定向次数过多或地址缺失。")
    except httpx.HTTPError:
        # Never include signed URLs, response bodies, or credentials in job errors/logs.
        raise JobError("PLATFORM_TRANSCRIPT_UNAVAILABLE", "读取平台文稿时网络连接失败，请稍后重试。") from None
