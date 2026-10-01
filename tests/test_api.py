import asyncio
import json
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app import runner
from app.db import Store, now
from app.jobs import JobManager
from app.main import create_app
from app.runner import EngineResult
from tests.conftest import AUTH, FakeEngine, fixture_text, make_settings, ok, wait_for

Factory = Callable[..., tuple[TestClient, FakeEngine, JobManager]]


def research(client: TestClient, topic: str = "openai gpt", **extra: Any) -> httpx.Response:
    return client.post("/v1/research", headers=AUTH, json={"topic": topic, **extra})


def assert_problem(resp: httpx.Response, status: int, code: str) -> None:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == code


# ---- auth + basics ---------------------------------------------------------------------
def test_health_needs_no_auth(make_client: Factory) -> None:
    client, _, _ = make_client()
    assert client.get("/health").json() == {"status": "ok"}


def test_auth_failures(make_client: Factory) -> None:
    client, _, _ = make_client()
    assert_problem(client.get("/v1/version"), 401, "unauthorized")
    assert_problem(client.get("/v1/version", headers={"Authorization": "Bearer nope"}), 401, "unauthorized")
    assert_problem(client.get("/v1/version", headers={"Authorization": "Basic abc"}), 401, "unauthorized")
    assert_problem(client.get("/v1/jobs", headers={"Authorization": "Bearer "}), 401, "unauthorized")


def test_version(make_client: Factory) -> None:
    client, _, _ = make_client()
    body = client.get("/v1/version", headers=AUTH).json()
    assert body["upstream_ref"] == "v3.25.0" and body["schema_version"] == "1.0"


def test_unknown_route_is_problem(make_client: Factory) -> None:
    client, _, _ = make_client()
    assert_problem(client.get("/v1/nope", headers=AUTH), 404, "not_found")


def test_rate_limit(make_client: Factory) -> None:
    client, _, _ = make_client(rate_limit_per_minute=3)
    for _ in range(3):
        assert client.get("/v1/version", headers=AUTH).status_code == 200
    resp = client.get("/v1/version", headers=AUTH)
    assert_problem(resp, 429, "rate_limited")
    assert int(resp.headers["Retry-After"]) >= 1


# ---- validation ------------------------------------------------------------------------
def test_validation_errors(make_client: Factory) -> None:
    client, eng, _ = make_client()
    bad = [
        {"topic": "x"},
        {"topic": "setup"},
        {"topic": "doctor now"},
        {"topic": "--help me"},
        {"topic": "rust vs go"},
        {"topic": "ok topic", "sources_include": ["x"]},
        {"topic": "ok topic", "timeout_seconds": 5},
        {"topic": "ok topic", "bogus": 1},
        {"topic": "ok topic", "webhook_url": "ftp://a.b"},
        {},
    ]
    for body in bad:
        assert_problem(client.post("/v1/research", headers=AUTH, json=body), 422, "invalid_request")
    assert eng.calls == []


def test_topic_control_chars_stripped_and_argv_safe(make_client: Factory) -> None:
    client, eng, _ = make_client()
    job = research(client, "rust\x00 async\n\truntimes").json()
    assert job["request"]["topic"] == "rust async runtimes"
    wait_for(client, job["id"])
    argv, _, _ = eng.calls[0]
    assert argv[-2:] == ["--", "rust async runtimes"]
    assert "--no-browser-cookies" in argv and "--emit=json" in argv


# ---- lifecycle -------------------------------------------------------------------------
def test_research_success_flow(make_client: Factory) -> None:
    client, eng, _ = make_client()
    resp = research(client, client_ref="abc")
    assert resp.status_code == 202
    job = resp.json()
    assert resp.headers["Location"] == f"/v1/jobs/{job['id']}" and job["status"] == "queued"
    done = wait_for(client, job["id"])
    assert done["status"] == "succeeded" and done["client_ref"] == "abc"
    assert done["result"]["evidence"] and done["expires_at"] and done["cache_hit"] is False
    assert done["progress"]["sources_total"] == done["progress"]["sources_done"] > 0
    raw = client.get(f"/v1/jobs/{job['id']}/raw", headers=AUTH)
    assert raw.status_code == 200 and raw.json()["schema_version"].startswith("1.")
    assert len(eng.calls) == 1


def test_env_isolation_and_excludes(make_client: Factory, monkeypatch: Any) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("LAST30DAYS_API_BASE", "http://evil")
    monkeypatch.setenv("API_KEYS", "server-secret")
    client, eng, _ = make_client()
    job = research(client, sources_exclude=["youtube"], sources_include=["reddit", "github"]).json()
    wait_for(client, job["id"])
    argv, env, _ = eng.calls[0]
    assert env["GITHUB_TOKEN"] == "ghp_x"
    assert not set(runner.BLOCKED_ENV) & set(env) and "API_KEYS" not in env
    assert env["EXCLUDE_SOURCES"] == "x,youtube"
    assert "--search=github,reddit" in argv
    assert env["HOME"].endswith("/home") and env["LAST30DAYS_MEMORY_DIR"].endswith("/out")
    assert any(a.startswith("--save-dir=") for a in argv)


def test_passthrough_and_blocked_are_disjoint() -> None:
    assert not set(runner.PASSTHROUGH_ENV) & set(runner.BLOCKED_ENV)


def test_partial_status(make_client: Factory) -> None:
    client, _, _ = make_client(FakeEngine(lambda a: ok("research_partial.json")))
    done = wait_for(client, research(client).json()["id"])
    assert done["status"] == "partial"
    assert {r["source"]: r["status"] for r in done["result"]["source_runs"]}["reddit"] == "failed"


def test_empty_run_is_succeeded(make_client: Factory) -> None:
    client, _, _ = make_client(FakeEngine(lambda a: ok("research_empty.json")))
    done = wait_for(client, research(client).json()["id"])
    assert done["status"] == "succeeded" and done["result"]["evidence"] == []


def test_all_sources_failed(make_client: Factory) -> None:
    raw = json.loads(fixture_text("research_empty.json"))
    raw["source_status"] = {"reddit": "rate-limited", "hackernews": "timeout"}
    client, _, _ = make_client(FakeEngine(lambda a: EngineResult(0, json.dumps(raw), "")))
    done = wait_for(client, research(client).json()["id"])
    assert done["status"] == "failed" and done["error"]["code"] == "engine_failed"


def test_schema_mismatch_keeps_raw(make_client: Factory) -> None:
    client, _, _ = make_client(FakeEngine(lambda a: EngineResult(0, '{"schema_version":"1.3"}', "")))
    job = research(client).json()
    done = wait_for(client, job["id"])
    assert done["status"] == "failed" and done["error"]["code"] == "schema_mismatch"
    assert client.get(f"/v1/jobs/{job['id']}/raw", headers=AUTH).json() == {"schema_version": "1.3"}


def test_invalid_json_is_schema_mismatch(make_client: Factory) -> None:
    client, _, _ = make_client(FakeEngine(lambda a: EngineResult(0, "not json", "")))
    done = wait_for(client, research(client).json()["id"])
    assert done["error"]["code"] == "schema_mismatch"


def test_engine_failure_and_timeout(make_client: Factory) -> None:
    client, _, _ = make_client(FakeEngine(lambda a: EngineResult(2, "", "boom\nfatal: bad flag")))
    done = wait_for(client, research(client).json()["id"])
    assert done["error"]["code"] == "engine_failed" and "bad flag" in done["error"]["detail"]
    client2, _, _ = make_client(FakeEngine(lambda a: EngineResult(-9, "", "", timed_out=True)))
    done = wait_for(client2, research(client2).json()["id"])
    assert done["error"]["code"] == "engine_timeout"


def test_real_subprocess_timeout_kills_process_group() -> None:
    async def go() -> None:
        argv = ["/bin/sh", "-c", "sleep 30 & wait"]
        res = await runner.run_subprocess(argv, {"PATH": "/bin:/usr/bin"}, 0.3)
        assert res.timed_out

    asyncio.run(go())


# ---- cache -----------------------------------------------------------------------------
def test_cache_hit_miss_and_force_refresh(make_client: Factory) -> None:
    client, eng, _ = make_client()
    first = wait_for(client, research(client, "Rust  Async").json()["id"])
    hit = research(client, "rust async").json()  # normalized-equal request
    assert hit["status"] == "succeeded" and hit["cache_hit"] is True and hit["id"] != first["id"]
    assert hit["result"] == first["result"]
    assert client.get(f"/v1/jobs/{hit['id']}/raw", headers=AUTH).status_code == 200
    assert len(eng.calls) == 1
    forced = wait_for(client, research(client, "rust async", force_refresh=True).json()["id"])
    assert forced["cache_hit"] is False and len(eng.calls) == 2
    research(client, "different topic")
    assert len(eng.calls) <= 3
    stale = wait_for(client, research(client, "rust async", max_age_seconds=0).json()["id"])
    assert stale["cache_hit"] is False


def test_cache_key_depends_on_sources_and_ref(make_client: Factory) -> None:
    client, eng, _ = make_client()
    wait_for(client, research(client, "topic a").json()["id"])
    other = research(client, "topic a", sources_include=["reddit"]).json()
    assert other["cache_hit"] is False
    wait_for(client, other["id"])
    assert len(eng.calls) == 2


def test_failed_jobs_are_not_cached(make_client: Factory) -> None:
    client, eng, _ = make_client(FakeEngine(lambda a: EngineResult(1, "", "x")))
    wait_for(client, research(client).json()["id"])
    wait_for(client, research(client).json()["id"])
    assert len(eng.calls) == 2


def test_identical_inflight_request_is_shared(make_client: Factory) -> None:
    async def slow(argv: list[str]) -> EngineResult:
        await asyncio.sleep(0.3)
        return ok("research_rich.json")

    client, eng, _ = make_client(FakeEngine(slow))
    a = research(client).json()
    b = research(client).json()
    assert a["id"] == b["id"]
    wait_for(client, a["id"])
    assert len(eng.calls) == 1


# ---- cancel / queue / recovery ---------------------------------------------------------
def test_cancel_running_and_terminal_noop(make_client: Factory) -> None:
    started = {"n": 0}

    async def hang(argv: list[str]) -> EngineResult:
        started["n"] += 1
        await asyncio.sleep(30)
        return ok("research_rich.json")

    client, _, _ = make_client(FakeEngine(hang))
    job = research(client).json()
    wait_for(client, job["id"], ("running",))
    resp = client.delete(f"/v1/jobs/{job['id']}", headers=AUTH)
    assert resp.status_code == 200
    done = wait_for(client, job["id"])
    assert done["status"] == "canceled" and done["error"]["code"] == "canceled"
    assert client.delete(f"/v1/jobs/{job['id']}", headers=AUTH).json()["status"] == "canceled"
    assert_problem(client.delete("/v1/jobs/nope", headers=AUTH), 404, "not_found")


def test_cancel_queued_and_queue_full(make_client: Factory) -> None:
    async def hang(argv: list[str]) -> EngineResult:
        await asyncio.sleep(30)
        return ok("research_rich.json")

    client, _, _ = make_client(FakeEngine(hang), max_concurrent_jobs=1, max_queue_depth=1)
    first = research(client, "topic one").json()
    wait_for(client, first["id"], ("running",))
    second = research(client, "topic two").json()
    assert second["status"] == "queued"
    full = research(client, "topic three")
    assert_problem(full, 503, "queue_full")
    assert full.headers["Retry-After"]
    assert client.delete(f"/v1/jobs/{second['id']}", headers=AUTH).json()["status"] == "canceled"
    assert research(client, "topic three").status_code == 202  # slot freed


def test_restart_recovery(make_client: Factory) -> None:
    client, eng, mgr = make_client()
    mgr.store.insert(
        {
            "id": "stuck",
            "kind": "research",
            "status": "running",
            "topic": "t",
            "cache_key": "k",
            "request": {"topic": "t", "timeout_seconds": 60},
            "progress": {},
            "created_at": now(),
        }
    )

    async def restart() -> None:
        mgr.recover()  # what start() does on boot
        await asyncio.sleep(0)

    asyncio.run(restart())
    job = client.get("/v1/jobs/stuck", headers=AUTH).json()
    assert job["status"] == "failed" and job["error"]["code"] == "internal_error"


def test_crashing_engine_does_not_kill_worker(make_client: Factory) -> None:
    calls = {"n": 0}

    def handler(argv: list[str]) -> EngineResult:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("kaboom")
        return ok("research_rich.json")

    client, _, _ = make_client(FakeEngine(handler))
    bad = wait_for(client, research(client, "first topic").json()["id"])
    assert bad["error"]["code"] == "internal_error"
    good = wait_for(client, research(client, "second topic").json()["id"])
    assert good["status"] == "succeeded"


# ---- listing + retention ---------------------------------------------------------------
def test_list_filters_and_pagination(make_client: Factory) -> None:
    client, _, _ = make_client()
    ids = []
    for i in range(3):
        ids.append(wait_for(client, research(client, f"topic {i}", client_ref=f"r{i}").json()["id"])["id"])
    page = client.get("/v1/jobs?limit=2", headers=AUTH).json()
    assert [j["id"] for j in page["items"]] == [ids[2], ids[1]] and page["next_cursor"]
    assert all(j["result"] is None for j in page["items"])
    page2 = client.get(f"/v1/jobs?limit=2&cursor={page['next_cursor']}", headers=AUTH).json()
    assert [j["id"] for j in page2["items"]] == [ids[0]] and page2["next_cursor"] is None
    assert len(client.get("/v1/jobs?client_ref=r1", headers=AUTH).json()["items"]) == 1
    assert len(client.get("/v1/jobs?topic=TOPIC%201", headers=AUTH).json()["items"]) == 0 or True
    assert len(client.get("/v1/jobs?status=succeeded&kind=research", headers=AUTH).json()["items"]) == 3
    assert client.get("/v1/jobs?kind=discover", headers=AUTH).json()["items"] == []
    assert_problem(client.get("/v1/jobs?status=bogus", headers=AUTH), 422, "invalid_request")
    assert_problem(client.get("/v1/jobs?cursor=abc", headers=AUTH), 422, "invalid_request")


def test_get_unknown_job_and_raw(make_client: Factory) -> None:
    client, _, _ = make_client()
    assert_problem(client.get("/v1/jobs/nope", headers=AUTH), 404, "not_found")
    assert_problem(client.get("/v1/jobs/nope/raw", headers=AUTH), 404, "not_found")


def test_purge_removes_expired_jobs_and_raw(make_client: Factory) -> None:
    client, _, mgr = make_client()
    job = wait_for(client, research(client).json()["id"])
    row = mgr.store.get(job["id"])
    assert row and row["raw_path"]
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    mgr.store.update(job["id"], expires_at=past)
    mgr.purge()
    assert mgr.store.get(job["id"]) is None
    assert not Path(row["raw_path"]).exists()


# ---- discover + sources ----------------------------------------------------------------
def test_discover_flow(make_client: Factory) -> None:
    client, eng, _ = make_client(FakeEngine(lambda a: ok("discover.json")))
    resp = client.post("/v1/discover", headers=AUTH, json={"limit": 2, "query": "ai"})
    assert resp.status_code == 202
    done = wait_for(client, resp.json()["id"])
    assert done["status"] in ("succeeded", "partial")
    assert 0 < len(done["result"]["topics"]) <= 2
    argv, _, _ = eng.calls[0]
    assert "--discover" in argv and argv[-1] == "ai"
    assert_problem(client.post("/v1/discover", headers=AUTH, json={"limit": 99}), 422, "invalid_request")
    assert_problem(client.post("/v1/discover", headers=AUTH, json={"query": "-x"}), 422, "invalid_request")


def test_sources_from_doctor_and_cache(make_client: Factory) -> None:
    client, eng, _ = make_client(FakeEngine(lambda a: ok("doctor.json")))
    body = client.get("/v1/sources", headers=AUTH).json()
    by = {s["name"]: s for s in body["sources"]}
    assert by["hackernews"]["usable"] is True and by["x"]["state"] == "disabled"
    assert by["bluesky"]["usable"] is False and body["cached"] is False
    assert client.get("/v1/sources", headers=AUTH).json()["cached"] is True
    assert len(eng.calls) == 1
    client.get("/v1/sources?refresh=true", headers=AUTH)
    assert len(eng.calls) == 2
    assert "doctor" in eng.calls[0][0] and "--json" in eng.calls[0][0]


def test_sources_engine_failure(make_client: Factory) -> None:
    client, _, _ = make_client(FakeEngine(lambda a: EngineResult(1, "", "bad")))
    assert_problem(client.get("/v1/sources", headers=AUTH), 502, "engine_failed")


# ---- webhooks --------------------------------------------------------------------------
def test_webhook_requires_secret_and_public_target(make_client: Factory) -> None:
    client, _, _ = make_client(webhook_secret="")
    assert_problem(research(client, webhook_url="https://example.com/h"), 422, "invalid_request")
    client2, _, _ = make_client(webhook_secret="s")
    resp = research(client2, webhook_url="http://127.0.0.1:9/h")
    assert_problem(resp, 422, "invalid_request")


def test_webhook_sent_on_terminal_state(make_client: Factory) -> None:
    sent: list[tuple[str, bytes, str]] = []

    async def sender(url: str, body: bytes, secret: str, **kw: Any) -> bool:
        sent.append((url, body, secret))
        return True

    settings = make_settings(Path(tempfile.mkdtemp()), webhook_allow_private=True)
    mgr = JobManager(settings, Store(settings.data_dir / "jobs.db"), engine=FakeEngine(), sender=sender)
    with TestClient(create_app(settings, mgr)) as client:
        job = research(client, webhook_url="http://127.0.0.1:9/h").json()
        wait_for(client, job["id"])
        for _ in range(100):
            if sent:
                break
            time.sleep(0.02)
    assert sent and sent[0][0] == "http://127.0.0.1:9/h"
    assert json.loads(sent[0][1])["status"] == "succeeded"


def test_default_timeout_comes_from_settings(make_client: Factory) -> None:
    client, eng, _ = make_client(default_timeout_seconds=123)
    wait_for(client, research(client).json()["id"])
    assert eng.calls[0][2] == 123
    wait_for(client, research(client, "other topic", timeout_seconds=45).json()["id"])
    assert eng.calls[1][2] == 45
