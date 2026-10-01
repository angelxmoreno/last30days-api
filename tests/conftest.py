import asyncio
import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Store
from app.jobs import JobManager
from app.main import create_app
from app.runner import EngineResult

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


class FakeEngine:
    """Stands in for the upstream CLI. `handler(argv)` returns an EngineResult (may be slow)."""

    def __init__(self, handler: Callable[[list[str]], Any] | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, str], float]] = []
        self.handler = handler or (lambda argv: ok("research_rich.json"))

    async def __call__(self, argv: list[str], env: dict[str, str], timeout: float) -> EngineResult:
        self.calls.append((argv, env, timeout))
        out = self.handler(argv)
        if asyncio.iscoroutine(out):
            out = await out
        return out  # type: ignore[no-any-return]


def ok(name: str) -> EngineResult:
    return EngineResult(0, fixture_text(name), "")


def make_settings(tmp_path: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "api_keys": frozenset({KEY}),
        "data_dir": tmp_path / "data",
        "upstream_ref": "v3.25.0",
        "max_concurrent_jobs": 2,
        "max_queue_depth": 20,
        "webhook_secret": "shh",
    }
    return Settings(**{**base, **kw})


@pytest.fixture
def make_client(tmp_path: Path) -> Iterator[Callable[..., tuple[TestClient, FakeEngine, JobManager]]]:
    clients: list[TestClient] = []

    def factory(engine: FakeEngine | None = None, **kw: Any) -> tuple[TestClient, FakeEngine, JobManager]:
        engine = engine or FakeEngine()
        settings = make_settings(tmp_path, **kw)
        mgr = JobManager(
            settings,
            Store(settings.data_dir / "jobs.db"),
            engine=engine,
            sender=kw.pop("sender", _noop_sender),
        )
        client = TestClient(create_app(settings, mgr))
        client.__enter__()
        clients.append(client)
        return client, engine, mgr

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


async def _noop_sender(*a: Any, **k: Any) -> bool:
    return True


def wait_for(client: TestClient, job_id: str, states: tuple[str, ...] | None = None) -> dict[str, Any]:
    terminal = states or ("succeeded", "partial", "failed", "canceled")
    for _ in range(300):
        job: dict[str, Any] = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
        if job["status"] in terminal:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} stuck in {job['status']}")
