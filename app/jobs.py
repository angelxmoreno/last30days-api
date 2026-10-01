"""Job manager: queue, bounded workers, cache, cancel, restart recovery, retention."""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app import runner
from app.config import SCHEMA_VERSION, UPSTREAM_REPO, Settings
from app.db import TERMINAL, Store, expiry, now
from app.errors import ApiError, problem
from app.models import (
    ALLOWED_SOURCES,
    DiscoverRequest,
    Job,
    Progress,
    ResearchRequest,
    SourceList,
    SourceStatus,
    UpstreamInfo,
)
from app.normalizer import SchemaMismatch, failure_summary, normalize_discover, normalize_research
from app.webhooks import deliver, is_private_target

log = logging.getLogger("jobs")
Engine = Callable[[list[str], dict[str, str], float], Awaitable[runner.EngineResult]]
Sender = Callable[..., Awaitable[bool]]
SOURCES_TTL = timedelta(minutes=10)


def cache_key(kind: str, request: dict[str, Any], upstream_ref: str) -> str:
    """Hash of the normalized request that determines the output, plus upstream + schema."""
    norm = {
        "kind": kind,
        "topic": " ".join(str(request.get("topic") or request.get("query") or "").lower().split()),
        "include": sorted(request.get("sources_include", [])),
        "exclude": sorted(request.get("sources_exclude", [])),
        "limit": request.get("limit"),
        "upstream": upstream_ref,
        "schema": SCHEMA_VERSION,
    }
    return hashlib.sha256(json.dumps(norm, sort_keys=True).encode()).hexdigest()


class JobManager:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        engine: Engine | None = None,
        sender: Sender = deliver,
    ) -> None:
        self.s = settings
        self.store = store
        self.engine: Engine = engine or self._default_engine
        self.sender = sender
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_requested: set[str] = set()
        self._bg: set[asyncio.Task[Any]] = set()
        self._workers: list[asyncio.Task[None]] = []
        self._sources: tuple[datetime, SourceList] | None = None
        self.raw_dir = settings.data_dir / "raw"
        self.work_dir = settings.data_dir / "work"
        ref = settings.upstream_ref
        self.upstream = UpstreamInfo(
            repo=UPSTREAM_REPO,
            ref=ref,
            version=ref[1:] if re.match(r"^v\d", ref) else None,
        )

    # ---- lifecycle -------------------------------------------------------------------
    async def start(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.recover()
        self.purge()
        self._workers = [asyncio.create_task(self._worker()) for _ in range(self.s.max_concurrent_jobs)]
        self._spawn(self._purge_loop())

    async def stop(self) -> None:
        for t in [*self._workers, *self._bg]:
            t.cancel()
        await asyncio.gather(*self._workers, *self._bg, return_exceptions=True)

    def recover(self) -> None:
        """Jobs left 'running' by a crash/restart become failed; 'queued' ones are re-queued."""
        for row in self.store.by_status("running"):
            self._finish(
                row["id"],
                "failed",
                error=problem(500, "internal_error", "server restarted while the job was running"),
            )
        for row in self.store.by_status("queued"):
            self.queue.put_nowait(row["id"])

    def purge(self) -> None:
        for raw in self.store.purge_expired():
            if raw and not self.store.raw_path_in_use(raw):
                Path(raw).unlink(missing_ok=True)

    async def _purge_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            self.purge()

    def _spawn(self, coro: Awaitable[Any]) -> None:
        t = asyncio.ensure_future(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ---- submit / cancel -------------------------------------------------------------
    async def submit(self, kind: str, req: ResearchRequest | DiscoverRequest) -> dict[str, Any]:
        request = req.model_dump()
        request["timeout_seconds"] = req.timeout_seconds or self.s.default_timeout_seconds
        if req.webhook_url:
            if not self.s.webhook_secret:
                raise ApiError(422, "invalid_request", "webhook_url needs WEBHOOK_SECRET set on the server")
            if not self.s.webhook_allow_private and await asyncio.to_thread(
                is_private_target, req.webhook_url
            ):
                raise ApiError(422, "invalid_request", "webhook_url resolves to a private address")
        key = cache_key(kind, request, self.s.upstream_ref)
        if not req.force_refresh:
            min_finished = (datetime.now(UTC) - timedelta(seconds=req.max_age_seconds)).isoformat()
            hit = self.store.find_cached(key, min_finished)
            if hit:
                return self._clone_cached(kind, request, key, hit)
            active = self.store.find_active(key)
            if active:
                return active
        if self.store.count("queued") >= self.s.max_queue_depth:
            raise ApiError(503, "queue_full", "job queue is full; retry later", headers={"Retry-After": "30"})
        job_id = uuid.uuid4().hex
        self.store.insert(
            {
                "id": job_id,
                "kind": kind,
                "status": "queued",
                "topic": request.get("topic") or request.get("query"),
                "client_ref": req.client_ref,
                "cache_key": key,
                "cache_hit": 0,
                "request": request,
                "progress": Progress().model_dump(),
                "created_at": now(),
            }
        )
        self.queue.put_nowait(job_id)
        return self.store.get(job_id) or {}

    def _clone_cached(
        self, kind: str, request: dict[str, Any], key: str, src: dict[str, Any]
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        ts = now()
        self.store.insert(
            {
                "id": job_id,
                "kind": kind,
                "status": src["status"],
                "topic": src["topic"],
                "client_ref": request.get("client_ref"),
                "cache_key": key,
                "cache_hit": 1,
                "request": request,
                "progress": src["progress"],
                "result": src["result"],
                "raw_path": src["raw_path"],
                "created_at": ts,
                "started_at": ts,
                "finished_at": ts,
                "expires_at": expiry(self.s.retention_days),
            }
        )
        self._spawn(self._notify(job_id))
        return self.store.get(job_id) or {}

    def cancel(self, job_id: str) -> dict[str, Any]:
        row = self.store.get(job_id)
        if row is None:
            raise ApiError(404, "not_found", f"job {job_id} not found")
        if row["status"] in TERMINAL:
            return row
        self._cancel_requested.add(job_id)
        if row["status"] == "queued":
            self._finish(job_id, "canceled", error=problem(409, "canceled", "canceled by client"))
        else:
            task = self._tasks.get(job_id)
            if task:
                task.cancel()
        return self.store.get(job_id) or row

    # ---- worker ----------------------------------------------------------------------
    async def _worker(self) -> None:
        while True:
            job_id = await self.queue.get()
            row = self.store.get(job_id)
            if row is None or row["status"] != "queued":
                continue  # canceled while waiting
            self.store.update(job_id, status="running", started_at=now())
            task = asyncio.create_task(self._run(row))
            self._tasks[job_id] = task
            try:
                await task
            except Exception:  # noqa: BLE001 - a bad job must never kill the worker
                log.exception("job %s crashed", job_id)
                self._finish(job_id, "failed", error=problem(500, "internal_error", "job crashed"))
            finally:
                self._tasks.pop(job_id, None)
                self._cancel_requested.discard(job_id)

    async def _run(self, row: dict[str, Any]) -> None:
        job_id, kind, req = row["id"], row["kind"], row["request"]
        job_dir = self.work_dir / job_id
        (job_dir / "home").mkdir(parents=True)
        (job_dir / "out").mkdir()
        try:
            excl = sorted({*self.s.sources_exclude_default, *req.get("sources_exclude", [])})
            env = runner.build_env(dict(os.environ), job_dir, excl)
            if kind == "research":
                include = [x for x in req["sources_include"] if x not in req["sources_exclude"]]
                argv = runner.research_argv(self.s, req["topic"], include, job_dir)
            else:
                argv = runner.discover_argv(self.s, req.get("query"), job_dir)
            try:
                res = await self.engine(argv, env, req["timeout_seconds"])
            except asyncio.CancelledError:
                if job_id in self._cancel_requested:
                    self._finish(job_id, "canceled", error=problem(409, "canceled", "canceled by client"))
                    return
                raise
            self._complete(row, res)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    def _complete(self, row: dict[str, Any], res: runner.EngineResult) -> None:
        job_id, kind, req = row["id"], row["kind"], row["request"]
        if res.timed_out:
            return self._finish(
                job_id,
                "failed",
                error=problem(504, "engine_timeout", f"engine exceeded {req['timeout_seconds']}s"),
            )
        if res.returncode != 0:
            tail = res.stderr.strip().splitlines()[-1:] or ["no output"]
            return self._finish(
                job_id,
                "failed",
                error=problem(502, "engine_failed", f"engine exited {res.returncode}: {tail[0][:300]}"),
            )
        raw_path = self.raw_dir / f"{job_id}.json"
        raw_path.write_text(res.stdout)
        try:
            parsed = json.loads(res.stdout)
            result = (
                normalize_research(parsed, self.upstream)
                if kind == "research"
                else normalize_discover(parsed, self.upstream, req["limit"])
            )
        except (json.JSONDecodeError, SchemaMismatch, ValueError) as exc:
            return self._finish(
                job_id,
                "failed",
                raw_path=str(raw_path),
                error=problem(
                    502, "schema_mismatch", f"engine output did not match expected schema: {exc}"[:500]
                ),
            )
        runs = result.source_runs
        failed, usable = failure_summary(runs)
        has_data = bool(getattr(result, "evidence", None) or getattr(result, "topics", None))
        if failed and not has_data and not usable:
            return self._finish(
                job_id,
                "failed",
                raw_path=str(raw_path),
                error=problem(502, "engine_failed", "every source failed"),
            )
        status = "partial" if failed else "succeeded"
        self._finish(
            job_id,
            status,
            raw_path=str(raw_path),
            result=result.model_dump(mode="json", by_alias=True),
            progress={"sources_total": len(runs), "sources_done": len(runs)},
        )

    def _finish(self, job_id: str, status: str, **fields: Any) -> None:
        self.store.update(
            job_id, status=status, finished_at=now(), expires_at=expiry(self.s.retention_days), **fields
        )
        self._spawn(self._notify(job_id))

    async def _notify(self, job_id: str) -> None:
        row = self.store.get(job_id)
        url = (row or {}).get("request", {}).get("webhook_url")
        if not row or not url or not self.s.webhook_secret:
            return
        body = to_job(row, with_result=True).model_dump_json(by_alias=True).encode()
        await self.sender(url, body, self.s.webhook_secret, allow_private=self.s.webhook_allow_private)

    # ---- engine + sources ------------------------------------------------------------
    @staticmethod
    async def _default_engine(argv: list[str], env: dict[str, str], timeout: float) -> runner.EngineResult:
        return await runner.run_subprocess(argv, env, timeout)

    async def sources(self, refresh: bool) -> SourceList:
        if not refresh and self._sources and datetime.now(UTC) - self._sources[0] < SOURCES_TTL:
            return self._sources[1].model_copy(update={"cached": True})
        job_dir = self.work_dir / f"doctor-{uuid.uuid4().hex}"
        (job_dir / "home").mkdir(parents=True)
        try:
            env = runner.build_env(dict(os.environ), job_dir)
            res = await self.engine(runner.doctor_argv(self.s, job_dir), env, 120)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)
        try:
            doctor = json.loads(res.stdout)["sources"]
            if res.returncode != 0 or not isinstance(doctor, dict):
                raise ValueError("bad doctor output")
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ApiError(502, "engine_failed", f"could not read source health: {exc}") from exc
        out = []
        for name in ALLOWED_SOURCES:
            info = doctor.get(name)
            state = str(info.get("audit_state", "unknown")) if isinstance(info, dict) else "unknown"
            detail = info.get("detail") if isinstance(info, dict) else None
            out.append(SourceStatus(name=name, usable=state == "working", state=state, detail=detail or None))
        out.append(
            SourceStatus(
                name="x", usable=False, state="disabled", detail="disabled by policy (cookie-based access)"
            )
        )
        listing = SourceList(sources=out, checked_at=datetime.now(UTC), cached=False)
        self._sources = (datetime.now(UTC), listing)
        return listing


def to_job(row: dict[str, Any], with_result: bool) -> Job:
    data = {
        k: row[k]
        for k in (
            "id",
            "kind",
            "status",
            "request",
            "cache_hit",
            "client_ref",
            "progress",
            "error",
            "created_at",
            "started_at",
            "finished_at",
            "expires_at",
        )
    }
    data["result"] = row.get("result") if with_result else None
    return Job.model_validate(data)
