import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .errors import JobError


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    """SQLite is both the task journal and a durable single-worker queue."""

    def __init__(self, db_path: Path):
        self.path = db_path

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, url TEXT NOT NULL, episode_id TEXT NOT NULL,
                    language TEXT NOT NULL, model TEXT NOT NULL,
                    status TEXT NOT NULL, stage TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT, error_message TEXT, episode_json TEXT, files_json TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(episode_id, language, model);
                CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, created_at);
            """)
            # Existing jobs keep their original local-only behavior after upgrading.
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            additions = {
                "transcript_mode": "TEXT NOT NULL DEFAULT 'local'",
                "download_audio": "INTEGER NOT NULL DEFAULT 1",
                "transcript_source": "TEXT",
                "source_note": "TEXT",
                "device": "TEXT",
                "compute_type": "TEXT",
                "asr_seconds": "REAL",
            }
            for name, definition in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")

    def recover(self):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status='QUEUED', stage='QUEUED', progress=0, updated_at=? WHERE status='RUNNING'",
                (utc_now(),),
            )
            db.execute(
                "UPDATE jobs SET status='CANCELLED', stage='CANCELLED', updated_at=? WHERE status='CANCEL_REQUESTED'",
                (utc_now(),),
            )

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("episode_json", "files_json"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def get(self, job_id):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def recent(self, limit=20):
        with self.connect() as db:
            return [
                self.decode(row)
                for row in db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
            ]

    def enqueue(
        self, url, episode_id, language, model, max_queued=20,
        transcript_mode="local", download_audio=True, device=None, compute_type=None,
    ):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM jobs WHERE episode_id=? AND language=? AND model=? "
                "AND transcript_mode=? AND download_audio=? AND device IS ? AND compute_type IS ? "
                "ORDER BY created_at DESC LIMIT 1",
                (episode_id, language, model, transcript_mode, int(download_audio), device, compute_type),
            ).fetchone()
            if existing:
                return self.decode(existing), True
            queued = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('QUEUED', 'RUNNING')").fetchone()[
                0
            ]
            if queued >= max_queued:
                raise JobError("QUEUE_FULL", "待处理任务已满，请稍后再试")
            job_id, now = uuid.uuid4().hex, utc_now()
            db.execute(
                "INSERT INTO jobs (id,url,episode_id,language,model,transcript_mode,download_audio,"
                "device,compute_type,status,stage,progress,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'QUEUED','QUEUED',0,?,?)",
                (job_id, url, episode_id, language, model, transcript_mode, int(download_audio),
                 device, compute_type, now, now),
            )
            return self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()), False

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE status='QUEUED' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE jobs SET status='RUNNING', stage='FETCHING_METADATA', updated_at=? WHERE id=?",
                    (utc_now(), row["id"]),
                )
            return self.decode(row)

    def retry(self, job_id):
        with self.connect() as db:
            result = db.execute(
                "UPDATE jobs SET status='QUEUED',stage='QUEUED',progress=0,error_code=NULL,"
                "error_message=NULL,source_note=NULL,updated_at=? WHERE id=? AND status IN ('FAILED','CANCELLED')",
                (utc_now(), job_id),
            )
            return result.rowcount > 0

    def cancel(self, job_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            status = row["status"]
            if status == "QUEUED":
                db.execute(
                    "UPDATE jobs SET status='CANCELLED',stage='CANCELLED',updated_at=? WHERE id=? AND status='QUEUED'",
                    (utc_now(), job_id),
                )
                return "CANCELLED"
            if status == "RUNNING":
                db.execute(
                    "UPDATE jobs SET status='CANCEL_REQUESTED',updated_at=? WHERE id=? AND status='RUNNING'",
                    (utc_now(), job_id),
                )
                return "CANCEL_REQUESTED"
            return status

    def complete(self, job_id, **fields):
        fields.update(status="COMPLETED", stage="DONE", progress=100)
        fields["updated_at"] = utc_now()
        allowed = {
            "status", "stage", "progress", "error_code", "error_message", "episode_json", "files_json",
            "transcript_source", "source_note", "updated_at",
        }
        if set(fields) - allowed:
            raise ValueError("Unsupported task field")
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = [
            json.dumps(value, ensure_ascii=False) if key.endswith("_json") else value
            for key, value in fields.items()
        ]
        with self.connect() as db:
            result = db.execute(
                f"UPDATE jobs SET {assignments} WHERE id=? AND status='RUNNING'", (*values, job_id)
            )
            return result.rowcount > 0

    def reset_missing_outputs(self, job_id, remaining_files):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status='QUEUED',stage='QUEUED',progress=0,files_json=?,updated_at=? WHERE id=? AND status='COMPLETED'",
                (json.dumps(remaining_files), utc_now(), job_id),
            )

    def update(self, job_id, **fields):
        allowed = {
            "status", "stage", "progress", "error_code", "error_message", "episode_json", "files_json",
            "transcript_source", "source_note", "asr_seconds",
        }
        if set(fields) - allowed:
            raise ValueError("Unsupported task field")
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = [
            json.dumps(value, ensure_ascii=False) if key.endswith("_json") else value
            for key, value in fields.items()
        ]
        with self.connect() as db:
            db.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*values, job_id))

    def asr_timings(self, model, device, compute_type):
        with self.connect() as db:
            rows = db.execute(
                "SELECT episode_json,asr_seconds FROM jobs WHERE status='COMPLETED' "
                "AND transcript_source='local' AND model=? AND device=? AND compute_type=? "
                "AND asr_seconds>0 ORDER BY updated_at DESC LIMIT 20", (model, device, compute_type),
            ).fetchall()
        ratios = []
        for row in rows:
            duration = (json.loads(row["episode_json"]) if row["episode_json"] else {}).get("duration")
            if duration and duration > 0:
                ratios.append(row["asr_seconds"] / duration)
        return ratios
