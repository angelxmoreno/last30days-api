"""Run the upstream CLI as a subprocess. argv list only, no shell, process-group kill."""

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings

# Vars copied from the server env into the engine env. Everything else is dropped.
# Anything in BLOCKED_ENV (documented in spec section 7) is never passed, by construction.
PASSTHROUGH_ENV = (
    "GITHUB_TOKEN",
    "BSKY_HANDLE",
    "BSKY_APP_PASSWORD",
    "BRAVE_API_KEY",
    "SCRAPECREATORS_API_KEY",
    "SERPER_API_KEY",
    "EXA_API_KEY",
    "PERPLEXITY_API_KEY",
    "LAST30DAYS_REDDIT_KEYLESS_RATE",
    "LAST30DAYS_REDDIT_BACKEND",
    "INCLUDE_SOURCES",
    "EXCLUDE_SOURCES",
    "LAST30DAYS_ENRICH_BUDGET_SECONDS",
    "LAST30DAYS_DOCTOR_PROBE_TIMEOUT",
    "LAST30DAYS_YT_SEARCH_TIMEOUT",
    "LAST30DAYS_TRANSCRIPT_TIMEOUT",
    "LAST30DAYS_SEARXNG_URL",
    "LAST30DAYS_STRICT_EXIT",
    "LAST30DAYS_DEBUG",
)
BLOCKED_ENV = (
    "LAST30DAYS_API_KEY",
    "LAST30DAYS_API_BASE",
    "FROM_BROWSER",
    "AUTH_TOKEN",
    "BROWSER_CDP_URL",
    "X_BEARER_TOKEN",
    "XAI_API_KEY",
    "XQUIK_API_KEY",
    "TRUTHSOCIAL_TOKEN",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "LAST30DAYS_CONFIG_DIR",
    "LAST30DAYS_CORPUS_DIRS",
    "LAST30DAYS_SKIP_KEYCHAIN",
    "LAST30DAYS_KEYCHAIN_ALIASES",
    "LAST30DAYS_PASS_PREFIX",
)
MAX_OUTPUT_BYTES = 20_000_000


@dataclass
class EngineResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def build_env(
    server_env: dict[str, str], job_dir: Path, extra_exclude: list[str] | None = None
) -> dict[str, str]:
    env = {k: server_env[k] for k in PASSTHROUGH_ENV if server_env.get(k)}
    if extra_exclude is not None:
        env["EXCLUDE_SOURCES"] = ",".join(extra_exclude)
    env["PATH"] = server_env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HOME"] = str(job_dir / "home")
    env["LAST30DAYS_MEMORY_DIR"] = str(job_dir / "out")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def research_argv(settings: Settings, topic: str, include: list[str], job_dir: Path) -> list[str]:
    script = str(settings.upstream_dir / "skills/last30days/scripts/last30days.py")
    argv = [
        settings.upstream_python,
        script,
        "--emit=json",
        "--no-browser-cookies",
        f"--save-dir={job_dir / 'out'}",
    ]
    if include:
        argv.append(f"--search={','.join(include)}")
    # '--' makes everything after it a positional topic, never a flag.
    return [*argv, "--", topic]


def discover_argv(settings: Settings, query: str | None, job_dir: Path) -> list[str]:
    script = str(settings.upstream_dir / "skills/last30days/scripts/last30days.py")
    argv = [
        settings.upstream_python,
        script,
        "--emit=json",
        "--no-browser-cookies",
        f"--save-dir={job_dir / 'out'}",
        "--discover",
    ]
    if query:
        argv.append(query)  # nargs='?': optional domain; validated not to start with '-'
    return argv


def doctor_argv(settings: Settings, job_dir: Path) -> list[str]:
    script = str(settings.upstream_dir / "skills/last30days/scripts/last30days.py")
    return [settings.upstream_python, script, "--no-browser-cookies", "doctor", "--json"]


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def run_subprocess(argv: list[str], env: dict[str, str], timeout: float) -> EngineResult:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # own process group so we can kill children too
        limit=MAX_OUTPUT_BYTES,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        _kill_group(proc)
        await proc.wait()
        return EngineResult(-9, "", "timed out", timed_out=True)
    except asyncio.CancelledError:
        _kill_group(proc)
        await proc.wait()
        raise
    return EngineResult(
        proc.returncode if proc.returncode is not None else -1,
        out[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"),
        err[-20000:].decode("utf-8", "replace"),
    )
