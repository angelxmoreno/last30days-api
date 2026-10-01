"""Settings, read once from environment variables."""

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

API_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0"
UPSTREAM_REPO = "https://github.com/mvanhorn/last30days-skill"
ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    api_keys: frozenset[str]
    data_dir: Path
    max_concurrent_jobs: int = 2
    max_queue_depth: int = 20
    default_timeout_seconds: int = 900
    retention_days: int = 30
    webhook_secret: str = ""
    webhook_allow_private: bool = False
    sources_exclude_default: tuple[str, ...] = ("x",)
    rate_limit_per_minute: int = 120
    upstream_ref: str = "unknown"
    upstream_dir: Path = Path("/opt/upstream")
    upstream_python: str = sys.executable


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    return int(raw) if raw else default


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    keys = frozenset(k.strip() for k in env.get("API_KEYS", "").split(",") if k.strip())
    if not keys:
        raise RuntimeError("API_KEYS is required (comma-separated list of bearer keys)")
    ref = env.get("UPSTREAM_REF", "").strip()
    if not ref and (ROOT / "UPSTREAM_REF").exists():
        ref = (ROOT / "UPSTREAM_REF").read_text().strip()
    excl = env.get("SOURCES_EXCLUDE_DEFAULT", "x")
    return Settings(
        api_keys=keys,
        data_dir=Path(env.get("DATA_DIR", "/data")),
        max_concurrent_jobs=_int(env, "MAX_CONCURRENT_JOBS", 2),
        max_queue_depth=_int(env, "MAX_QUEUE_DEPTH", 20),
        default_timeout_seconds=_int(env, "DEFAULT_TIMEOUT_SECONDS", 900),
        retention_days=_int(env, "RETENTION_DAYS", 30),
        webhook_secret=env.get("WEBHOOK_SECRET", ""),
        webhook_allow_private=env.get("WEBHOOK_ALLOW_PRIVATE", "").lower() == "true",
        sources_exclude_default=tuple(s.strip() for s in excl.split(",") if s.strip()),
        rate_limit_per_minute=_int(env, "RATE_LIMIT_PER_MINUTE", 120),
        upstream_ref=ref or "unknown",
        upstream_dir=Path(env.get("UPSTREAM_DIR", "/opt/upstream")),
        upstream_python=env.get("UPSTREAM_PYTHON", sys.executable),
    )
