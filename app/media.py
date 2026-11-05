import ipaddress
import socket
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from .config import Settings
from .errors import JobError
from .xiaoyuzhou import HEADERS

EXTENSIONS = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
}


def validate_audio_url(url: str, allowed_hosts: tuple[str, ...]):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or not valid_port:
        raise JobError("UNSAFE_AUDIO_URL", "音频地址必须为合法的 HTTPS 地址")
    allowed = any(
        host == pattern.lstrip(".") or (pattern.startswith(".") and host.endswith(pattern))
        for pattern in allowed_hosts
    )
    if not allowed:
        raise JobError(
            "UNSUPPORTED_AUDIO_HOST", f"暂不支持该音频域名：{host}。可在 AUDIO_ALLOWED_HOSTS 中配置可信域名。"
        )
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise JobError("UNSAFE_AUDIO_URL", "音频域名不能指向本机或内网地址")
    except socket.gaierror as exc:
        raise JobError("AUDIO_DOWNLOAD_FAILED", "音频域名解析失败") from exc


def probe_audio(path: Path, max_duration: int) -> float:
    import av

    try:
        with av.open(str(path)) as container:
            if not container.streams.audio:
                raise ValueError("No audio stream")
            duration = container.duration / av.time_base if container.duration else None
            if duration is None:
                stream = container.streams.audio[0]
                duration = float(stream.duration * stream.time_base) if stream.duration else None
            if not duration or duration <= 0:
                raise ValueError("Unknown duration")
            if duration > max_duration:
                raise JobError("AUDIO_TOO_LONG", f"音频超过 {max_duration // 60} 分钟限制")
            return float(duration)
    except JobError:
        raise
    except Exception as exc:
        raise JobError("INVALID_AUDIO", "下载文件不是可识别的完整音频") from exc


def download_audio(episode, directory: Path, settings: Settings, on_progress, check_stop) -> Path:
    """Write to .part and rename only after a complete response. Validate every redirect."""
    if episode.size and episode.size > settings.max_audio_bytes:
        raise JobError("AUDIO_TOO_LARGE", "音频超过文件大小限制")
    temporary = directory / "audio.part"
    for attempt in range(3):
        check_stop()
        try:
            url = episode.audio_url
            with httpx.Client(
                timeout=httpx.Timeout(60, connect=15), follow_redirects=False, headers=HEADERS
            ) as client:
                for _ in range(6):
                    validate_audio_url(url, settings.audio_hosts)
                    with client.stream("GET", url) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise JobError("AUDIO_DOWNLOAD_FAILED", "音频重定向地址缺失")
                            url = urljoin(url, location)
                            continue
                        response.raise_for_status()
                        mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
                        if not (
                            mime.startswith("audio/") or mime in {"application/octet-stream", "video/mp4", ""}
                        ):
                            raise JobError("INVALID_AUDIO", "服务器没有返回音频文件")
                        total = int(response.headers.get("content-length", "0") or 0)
                        if total > settings.max_audio_bytes:
                            raise JobError("AUDIO_TOO_LARGE", "音频超过文件大小限制")
                        suffix = EXTENSIONS.get(mime)
                        if not suffix:
                            candidate = Path(urlparse(url).path).suffix.lower()
                            suffix = candidate if candidate in set(EXTENSIONS.values()) else ".m4a"
                        count = 0
                        with temporary.open("wb") as output:
                            for chunk in response.iter_bytes(256 * 1024):
                                check_stop()
                                count += len(chunk)
                                if count > settings.max_audio_bytes:
                                    raise JobError("AUDIO_TOO_LARGE", "音频超过文件大小限制")
                                output.write(chunk)
                                on_progress(count, total or episode.size or 0)
                        if count == 0 or (total and count != total):
                            raise JobError("INCOMPLETE_DOWNLOAD", "音频下载不完整，请重试")
                        result = directory / f"audio{suffix}"
                        probe_audio(temporary, settings.max_duration_seconds)
                        temporary.replace(result)
                        return result
                raise JobError("AUDIO_DOWNLOAD_FAILED", "音频重定向次数过多")
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            if attempt == 2:
                raise JobError("AUDIO_DOWNLOAD_FAILED", "音频下载超时或连接中断，请重试") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500 or attempt == 2:
                raise JobError(
                    "AUDIO_DOWNLOAD_FAILED", f"音频下载失败（HTTP {exc.response.status_code}）"
                ) from exc
        finally:
            temporary.unlink(missing_ok=True)
    raise JobError("AUDIO_DOWNLOAD_FAILED", "音频下载失败")
