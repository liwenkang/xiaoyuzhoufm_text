"""Single-machine defaults; generated data stays under storage/."""

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    storage_dir: Path = field(
        default_factory=lambda: Path(os.getenv("STORAGE_DIR", BASE_DIR / "storage")).resolve()
    )
    whisper_model: str = field(default_factory=lambda: os.getenv("WHISPER_MODEL", "small"))
    whisper_device: str = field(default_factory=lambda: os.getenv("WHISPER_DEVICE", "cpu"))
    whisper_compute_type: str = field(default_factory=lambda: os.getenv("WHISPER_COMPUTE_TYPE", "int8"))
    transcript_mode: str = field(default_factory=lambda: os.getenv("TRANSCRIPT_MODE", "platform"))
    xiaoyuzhou_access_token: str = field(
        default_factory=lambda: os.getenv("XIAOYUZHOU_ACCESS_TOKEN", ""), repr=False
    )
    xiaoyuzhou_token_file: Path = field(
        default_factory=lambda: Path(
            os.getenv("XIAOYUZHOU_TOKEN_FILE", BASE_DIR / ".secrets" / "xiaoyuzhou-access-token.txt")
        ).resolve()
    )
    max_transcript_bytes: int = 16 * 1024 * 1024
    max_audio_bytes: int = field(default_factory=lambda: int(os.getenv("MAX_AUDIO_BYTES", str(1024**3))))
    max_duration_seconds: int = field(default_factory=lambda: int(os.getenv("MAX_DURATION_SECONDS", "14400")))
    max_queued_jobs: int = 20
    audio_hosts: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            host.strip().lower()
            for host in os.getenv("AUDIO_ALLOWED_HOSTS", ".xyzcdn.net").split(",")
            if host.strip()
        )
    )

    def __post_init__(self):
        if self.transcript_mode == "auto":
            self.transcript_mode = "platform"
        if self.transcript_mode not in {"platform", "local"}:
            raise ValueError("TRANSCRIPT_MODE must be platform or local")

    @property
    def db_path(self) -> Path:
        return self.storage_dir / "jobs.sqlite3"
