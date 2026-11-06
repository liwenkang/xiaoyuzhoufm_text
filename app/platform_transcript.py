"""Read existing Xiaoyuzhou transcripts using a server-side access token.

The endpoint is not a documented public API. Keep its protocol isolated here.
Credentials are sent only to the fixed API origin, never to transcript/CDN URLs.
"""

import ipaddress
import json
import math
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from .errors import AppError

API_URL = "https://api.xiaoyuzhoufm.com/v1/episode-transcript/get"
APP_USER_AGENT = "Xiaoyuzhou/2.7.0 (build:1234; iOS 17.0.0)"
MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None


@dataclass
class PlatformTranscript:
    segments: list[Segment]
    end_timestamps_inferred: bool


def validate_access_token(token):
    token = token.strip()
    if not token or len(token) > 16384 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise AppError("PLATFORM_AUTH_INVALID", "请填写有效的 access token，仅粘贴 token 值，不要包含请求头名称或空格。")
    return token


def _read_json(response, limit):
    chunks, size = [], 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise AppError("PLATFORM_TRANSCRIPT_INVALID", "平台文稿响应过大，已停止读取。")
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except (ValueError, UnicodeError, RecursionError):
        raise AppError("PLATFORM_TRANSCRIPT_INVALID", "平台文稿不是有效的 JSON 数据。") from None


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
        raise AppError(
            "PLATFORM_TRANSCRIPT_INVALID", "平台文稿为空或时间戳格式不受支持，未将其作为完整文稿保存。"
        ) from None


def _fetch_transcript_url(episode, token):
    if not episode.transcript_media_id:
        raise AppError("PLATFORM_TRANSCRIPT_MISSING", "小宇宙公开页面未提供这期节目的文稿标识。")
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
                    raise AppError(
                        "PLATFORM_AUTH_EXPIRED", "小宇宙登录凭据已失效或无效，请更新自己的 access token。"
                    )
                if response.status_code == 403:
                    raise AppError(
                        "PLATFORM_AUTH_FORBIDDEN", "小宇宙拒绝了文稿访问，可能没有权限；无法据此确认 token 是否过期。"
                    )
                if response.status_code == 404:
                    raise AppError("PLATFORM_TRANSCRIPT_MISSING", "小宇宙暂未提供这期的可用文稿。")
                if response.status_code != 200:
                    raise AppError("PLATFORM_TRANSCRIPT_UNAVAILABLE", "小宇宙文稿接口暂不可用，请稍后再试。")
                payload = _read_json(response, 256 * 1024)
        data = payload.get("data") if isinstance(payload, dict) else None
        url = data.get("transcriptUrl") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url:
            raise AppError("PLATFORM_TRANSCRIPT_MISSING", "小宇宙暂未返回这期的可用文稿地址。")
        return url
    except httpx.HTTPError:
        raise AppError("PLATFORM_TRANSCRIPT_UNAVAILABLE", "读取平台文稿时网络连接失败，请稍后重试。") from None


def validate_transcript_url(url):
    """Allow only HTTPS CDN hosts with public DNS, including on redirects."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if (
        parsed.scheme != "https" or not valid_port or parsed.username or parsed.password
        or not (host == "xyzcdn.net" or host.endswith(".xyzcdn.net"))
    ):
        raise AppError("PLATFORM_TRANSCRIPT_UNSAFE_URL", "平台返回的文稿地址不在可信 HTTPS 域名范围内。")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise AppError("PLATFORM_TRANSCRIPT_UNSAFE_URL", "文稿域名不能指向本机或内网地址。")
    except socket.gaierror:
        raise AppError("PLATFORM_TRANSCRIPT_UNAVAILABLE", "文稿域名解析失败，请稍后重试。") from None


def fetch_platform_transcript(episode, access_token, max_bytes=MAX_TRANSCRIPT_BYTES):
    if not episode.transcript_media_id:
        raise AppError("PLATFORM_TRANSCRIPT_MISSING", "小宇宙公开页面未提供这期节目的文稿标识。")
    token = validate_access_token(access_token)
    url = _fetch_transcript_url(episode, token)
    try:
        # A new client also avoids carrying API cookies to the CDN.
        with httpx.Client(
            timeout=httpx.Timeout(20, connect=10), follow_redirects=False,
            headers={"User-Agent": APP_USER_AGENT},
        ) as client:
            for _ in range(4):
                validate_transcript_url(url)
                with client.stream("GET", url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            break
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        raise AppError("PLATFORM_TRANSCRIPT_UNAVAILABLE", "平台文稿文件暂不可读取。")
                    return parse_segments(
                        _read_json(response, max_bytes), episode.duration
                    )
        raise AppError("PLATFORM_TRANSCRIPT_UNAVAILABLE", "平台文稿文件重定向次数过多或地址缺失。")
    except httpx.HTTPError:
        # Never include signed URLs, response bodies, or credentials in errors/logs.
        raise AppError("PLATFORM_TRANSCRIPT_UNAVAILABLE", "读取平台文稿时网络连接失败，请稍后重试。") from None
