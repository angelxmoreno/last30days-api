"""FastAPI app: routes, auth, rate limiting."""

import hmac
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import API_VERSION, SCHEMA_VERSION, UPSTREAM_REPO, Settings, load_settings
from app.db import Store
from app.errors import ApiError, install_handlers
from app.jobs import JobManager, to_job
from app.models import (
    DiscoverRequest,
    Job,
    JobList,
    JobSummary,
    Problem,
    ResearchRequest,
    SourceList,
    VersionInfo,
)

bearer = HTTPBearer(auto_error=False, description="Static API key from API_KEYS")
PROBLEM = {"model": Problem, "content": {"application/problem+json": {}}}
ERRORS: dict[int | str, dict[str, Any]] = {
    401: {**PROBLEM, "description": "Missing or invalid API key"},
    429: {**PROBLEM, "description": "Rate limit exceeded"},
}


class RateLimiter:
    """Sliding one-minute window per API key (in memory; one process)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now, q = time.monotonic(), self.hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            retry = max(1, int(60 - (now - q[0])) + 1)
            raise ApiError(429, "rate_limited", "too many requests", headers={"Retry-After": str(retry)})
        q.append(now)


def create_app(settings: Settings | None = None, manager: JobManager | None = None) -> FastAPI:
    settings = settings or load_settings()
    limiter = RateLimiter(settings.rate_limit_per_minute)
    mgr = manager or JobManager(settings, Store(settings.data_dir / "jobs.db"))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await mgr.start()
        yield
        await mgr.stop()

    app = FastAPI(
        title="last30days API",
        version=API_VERSION,
        summary="Private API for scored social/web evidence on a topic",
        description=(
            "Async research jobs over the last30days engine. Authenticate with "
            "`Authorization: Bearer <key>`. Errors are `application/problem+json` with a stable "
            "`code`. Additive changes are allowed within /v1; breaking changes need /v2."
        ),
        lifespan=lifespan,
        license_info={"name": "MIT", "identifier": "MIT"},
        servers=[{"url": "http://localhost:8000", "description": "Local / compose default"}],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_handlers(app)

    def auth(creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> str:
        token = creds.credentials if creds else ""
        if not any(hmac.compare_digest(token, k) for k in settings.api_keys) or not token:
            raise ApiError(
                401, "unauthorized", "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"}
            )
        limiter.check(token)
        return token

    Auth = Depends(auth)

    @app.get(
        "/health",
        operation_id="getHealth",
        tags=["meta"],
        summary="Liveness (no auth)",
        openapi_extra={"security": []},
    )
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/v1/version",
        operation_id="getVersion",
        tags=["meta"],
        response_model=VersionInfo,
        dependencies=[Auth],
        responses=ERRORS,
    )
    async def version() -> VersionInfo:
        return VersionInfo(
            api_version=API_VERSION,
            schema_version=SCHEMA_VERSION,
            upstream_ref=settings.upstream_ref,
            upstream_repo=UPSTREAM_REPO,
        )

    @app.get(
        "/v1/sources",
        operation_id="listSources",
        tags=["meta"],
        response_model=SourceList,
        dependencies=[Auth],
        responses={**ERRORS, 502: PROBLEM},
    )
    async def sources(refresh: bool = False) -> SourceList:
        return await mgr.sources(refresh)

    def accepted(row: dict[str, Any], response: Response) -> Job:
        response.status_code = 202
        response.headers["Location"] = f"/v1/jobs/{row['id']}"
        return to_job(row, with_result=True)

    submit_errors = {**ERRORS, 422: PROBLEM, 503: PROBLEM}

    @app.post(
        "/v1/research",
        operation_id="createResearchJob",
        tags=["jobs"],
        status_code=202,
        response_model=Job,
        dependencies=[Auth],
        responses=submit_errors,
        summary="Start or reuse a research job",
    )
    async def create_research(body: ResearchRequest, response: Response) -> Job:
        return accepted(await mgr.submit("research", body), response)

    @app.post(
        "/v1/discover",
        operation_id="createDiscoverJob",
        tags=["jobs"],
        status_code=202,
        response_model=Job,
        dependencies=[Auth],
        responses=submit_errors,
        summary="Start or reuse a topic-less trending job",
    )
    async def create_discover(body: DiscoverRequest, response: Response) -> Job:
        return accepted(await mgr.submit("discover", body), response)

    @app.get(
        "/v1/jobs",
        operation_id="listJobs",
        tags=["jobs"],
        response_model=JobList,
        dependencies=[Auth],
        responses={**ERRORS, 422: PROBLEM},
    )
    async def list_jobs(
        status: Annotated[
            str | None, Query(pattern="^(queued|running|succeeded|partial|failed|canceled)$")
        ] = None,
        kind: Annotated[str | None, Query(pattern="^(research|discover)$")] = None,
        topic: Annotated[str | None, Query(max_length=300)] = None,
        client_ref: Annotated[str | None, Query(max_length=200)] = None,
        cursor: Annotated[str | None, Query(max_length=40)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> JobList:
        before = None
        if cursor:
            if not cursor.isdigit():
                raise ApiError(422, "invalid_request", "cursor: malformed")
            before = int(cursor)
        rows = mgr.store.list_jobs(
            status=status, kind=kind, topic=topic, client_ref=client_ref, before_seq=before, limit=limit + 1
        )
        page = rows[:limit]
        nxt = str(page[-1]["seq"]) if len(rows) > limit else None
        return JobList(
            items=[JobSummary(**to_job(r, with_result=False).model_dump()) for r in page], next_cursor=nxt
        )

    def load(job_id: str) -> dict[str, Any]:
        row = mgr.store.get(job_id)
        if row is None:
            raise ApiError(404, "not_found", f"job {job_id} not found")
        return row

    NOT_FOUND = {**ERRORS, 404: PROBLEM}

    @app.get(
        "/v1/jobs/{job_id}",
        operation_id="getJob",
        tags=["jobs"],
        response_model=Job,
        dependencies=[Auth],
        responses=NOT_FOUND,
    )
    async def get_job(job_id: str) -> Job:
        return to_job(load(job_id), with_result=True)

    @app.delete(
        "/v1/jobs/{job_id}",
        operation_id="cancelJob",
        tags=["jobs"],
        response_model=Job,
        dependencies=[Auth],
        responses=NOT_FOUND,
        summary="Cancel (no-op if terminal)",
    )
    async def cancel_job(job_id: str) -> Job:
        return to_job(mgr.cancel(job_id), with_result=True)

    @app.get(
        "/v1/jobs/{job_id}/raw",
        operation_id="getJobRaw",
        tags=["jobs"],
        dependencies=[Auth],
        responses={
            **NOT_FOUND,
            200: {
                "description": "Unmodified upstream JSON (no compatibility guarantee)",
                "content": {"application/json": {"schema": {"type": "object"}}},
            },
        },
        response_class=JSONResponse,
    )
    async def get_raw(job_id: str) -> Response:
        row = load(job_id)
        path = row.get("raw_path")
        if not path or not Path(path).exists():
            raise ApiError(404, "not_found", "no raw output stored for this job")
        return FileResponse(path, media_type="application/json")

    @app.middleware("http")
    async def no_store(request: Request, call_next: Any) -> Response:
        resp: Response = await call_next(request)
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    return app
