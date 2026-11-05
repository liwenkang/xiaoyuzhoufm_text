import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from .errors import JobCancelled, JobError, JobInterrupted


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None


class FasterWhisperTranscriber:
    """Lazy-load one model per worker and reuse it across episodes."""

    def __init__(self, settings):
        self.settings = settings
        self.model = None
        self.model_name = None
        self.device = settings.whisper_device
        self.compute_type = settings.whisper_compute_type
        self.model_key = None

    def configure(self, device, compute_type):
        self.device, self.compute_type = device, compute_type

    def load_model(self, model_name):
        key = (model_name, self.device, self.compute_type)
        if self.model is not None and self.model_key == key:
            return
        try:
            # Standard HTTPS downloads are more reliable than the optional Xet transport on local PCs.
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
            os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
            import ctranslate2
            from faster_whisper import WhisperModel

            device = self.device
            if device == "auto":
                device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
            compute_type = self.compute_type
            if compute_type == "auto":
                compute_type = "float16" if device == "cuda" else "int8"
            self.model = None  # Release the previous model before loading another configuration.
            self.model = WhisperModel(
                model_name,
                device=device,
                compute_type=compute_type,
                download_root=str(self.settings.storage_dir / "models"),
                cpu_threads=min(8, os.cpu_count() or 4),
            )
            self.model_name = model_name
            self.model_key = key
        except Exception as exc:
            raise JobError(
                "MODEL_LOAD_FAILED",
                "语音模型加载失败。首次使用需联网下载模型；也可以将 WHISPER_MODEL 设置为本地 CTranslate2 模型目录。详情见服务日志。",
            ) from exc

    def transcribe(self, audio_path, language, duration, title, on_progress, check_stop):
        try:
            chunks, info = self.model.transcribe(
                str(audio_path),
                language=None if language == "auto" else language,
                vad_filter=True,
                beam_size=5,
                initial_prompt=f"简体中文。节目名称：{title[:160]}" if language in ("zh", "auto") else None,
            )
            result = []
            for item in chunks:
                check_stop()
                text = item.text.strip()
                if text:
                    result.append(Segment(round(float(item.start), 3), round(float(item.end), 3), text))
                on_progress(min(float(item.end) / max(duration, 1), 1))
            if not result:
                raise JobError("NO_SPEECH", "没有识别到可转写的人声，音频仍可下载")
            return result, info.language
        except JobError:
            raise
        except Exception as exc:
            if isinstance(exc, (JobInterrupted, JobCancelled)):
                raise
            raise JobError(
                "ASR_FAILED", "语音识别失败，音频仍可下载。可重试或更换模型；详情见服务日志。"
            ) from exc


def timestamp(seconds, separator=","):
    value = max(0, round(seconds * 1000))
    hours, value = divmod(value, 3600000)
    minutes, value = divmod(value, 60000)
    seconds, milliseconds = divmod(value, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{milliseconds:03d}"


def write_transcript(
    segments: list[Segment], directory: Path, episode: dict, language: str, model: str | None,
    source="local", end_timestamps_inferred=False,
):
    text = "\n".join(segment.text for segment in segments) + "\n"
    title = episode["title"].replace("\n", " ")
    markdown = f"# {title}\n\n节目：{episode.get('podcast_title') or '未命名播客'}\n\n来源：{episode['source_url']}\n\n"
    if source == "xiaoyuzhou":
        markdown += "> 文稿来源：小宇宙平台已有文稿；未在本机重新识别。请结合原音频核对。\n\n"
    else:
        markdown += "> 本文由本机自动语音识别生成，可能存在错字和时间偏差，请结合原音频核对。\n\n"
    if end_timestamps_inferred:
        markdown += "> 平台未提供的结束时间由下一段开始时间或节目时长推算。\n\n"
    markdown += (
        "\n\n".join(f"**[{timestamp(segment.start, '.')[:-4]}]** {segment.text}" for segment in segments)
        + "\n"
    )
    srt = (
        "\n\n".join(
            f"{i}\n{timestamp(segment.start)} --> {timestamp(segment.end)}\n{segment.text}"
            for i, segment in enumerate(segments, 1)
        )
        + "\n"
    )
    vtt = (
        "WEBVTT\n\n"
        + "\n\n".join(
            f"{timestamp(segment.start, '.')} --> {timestamp(segment.end, '.')}\n{segment.text}"
            for segment in segments
        )
        + "\n"
    )
    data = {
        "episode_id": episode["episode_id"],
        "title": episode["title"],
        "podcast_title": episode.get("podcast_title"),
        "source_url": episode["source_url"],
        "duration": episode.get("duration"),
        "language": language,
        "model": model,
        "transcript_source": source,
        "end_timestamps_inferred": end_timestamps_inferred,
        "segments": [asdict(segment) for segment in segments],
    }
    contents = {
        "txt": text,
        "md": markdown,
        "srt": srt,
        "vtt": vtt,
        "json": json.dumps(data, ensure_ascii=False, indent=2),
    }
    files = {}
    for kind, content in contents.items():
        name = f"transcript.{kind}"
        temporary = directory / f"{name}.part"
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(directory / name)
        files[kind] = name
    return files
