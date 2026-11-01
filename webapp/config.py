"""Settings, read once from the environment.

Database: either DATABASE_URL (a postgres:// URI) or the standard libpq variables
(PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE, PGSSLMODE).
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("PP_ROOT", Path(__file__).resolve().parent.parent))


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))
    environment: str = field(default_factory=lambda: os.environ.get("PP_ENV", "local"))       # local | modal
    runner: str = field(default_factory=lambda: os.environ.get("PP_RUNNER", "local"))          # local | docker | modal
    tasks_dir: Path = field(default_factory=lambda: Path(os.environ.get("PP_TASKS_DIR", ROOT / "tasks")))
    benchmark_dir: Path = field(default_factory=lambda: Path(os.environ.get("PP_BENCHMARK_DIR", ROOT / "benchmark")))
    base_url: str = field(default_factory=lambda: os.environ.get("PP_BASE_URL", "").rstrip("/"))
    signup: str = field(default_factory=lambda: os.environ.get("PP_SIGNUP", "open"))           # open | closed
    cookie_secure: bool = field(default_factory=lambda: _bool("PP_COOKIE_SECURE", False))
    demo_daily_run_cap: int = field(default_factory=lambda: int(os.environ.get("PP_DEMO_DAILY_RUN_CAP", "40")))
    max_upload_mb: int = field(default_factory=lambda: int(os.environ.get("PP_MAX_UPLOAD_MB", "20")))
    contact_email: str = field(default_factory=lambda: os.environ.get("PP_CONTACT_EMAIL", ""))
    secret: str = field(default_factory=lambda: os.environ.get("PP_SECRET", ""))

    def app_secret(self) -> bytes:
        """HMAC key for non-session signatures. Falls back to a value derived from the DB credentials."""
        seed = self.secret or self.database_url or os.environ.get("PGPASSWORD", "") or "pp-local-dev"
        return hashlib.sha256(("pipelineproof:" + seed).encode()).digest()


settings = Settings()
