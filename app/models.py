"""API models. They mirror openapi.yaml (CI fails if they drift)."""

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

JobStatus = Literal["queued", "running", "succeeded", "partial", "failed", "canceled"]
JobKind = Literal["research", "discover"]
EvidenceKind = Literal["post", "comment", "video", "transcript_excerpt", "market", "repo", "article", "other"]

# Upstream treats these first words as subcommands, not topics (setup would install software).
RESERVED_FIRST_WORDS = {"setup", "doctor", "library", "queue", "welcome"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Source names we allow clients to name. X is deliberately absent (disabled).
ALLOWED_SOURCES = (
    "reddit",
    "hackernews",
    "polymarket",
    "github",
    "youtube",
    "bluesky",
    "web",
    "tiktok",
    "instagram",
)


def clean_text(value: str) -> str:
    return " ".join(_CONTROL.sub(" ", value).split())


class Problem(BaseModel):
    type: str
    title: str
    status: int
    code: str
    detail: str


class _RunOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_age_seconds: int = Field(21600, ge=0, le=2_592_000)
    force_refresh: bool = False
    timeout_seconds: int | None = Field(None, ge=30, le=1800)  # None = DEFAULT_TIMEOUT_SECONDS
    webhook_url: str | None = Field(None, max_length=2000)
    client_ref: str | None = Field(None, max_length=200)

    @field_validator("webhook_url")
    @classmethod
    def _http_only(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^https?://[^\s/]+", v):
            raise ValueError("must be an http(s) URL")
        return v


class ResearchRequest(_RunOptions):
    topic: str = Field(min_length=2, max_length=300)
    sources_include: list[str] = []
    sources_exclude: list[str] = []

    @field_validator("topic")
    @classmethod
    def _topic(cls, v: str) -> str:
        v = clean_text(v)
        if len(v) < 2:
            raise ValueError("topic too short")
        first = v.split(" ", 1)[0].lower()
        if first in RESERVED_FIRST_WORDS:
            raise ValueError(f"topic may not start with reserved word '{first}'")
        if v.startswith("-"):
            raise ValueError("topic may not start with '-'")
        if re.search(r"\s(vs\.?|versus)\s", v, re.IGNORECASE):
            raise ValueError("comparison topics ('X vs Y') are not supported; run one job per topic")
        return v

    @field_validator("sources_include", "sources_exclude")
    @classmethod
    def _sources(cls, v: list[str]) -> list[str]:
        out = sorted({s.strip().lower() for s in v})
        bad = [s for s in out if s not in ALLOWED_SOURCES]
        if bad:
            raise ValueError(f"unknown source(s) {bad}; allowed: {list(ALLOWED_SOURCES)}")
        return out


class DiscoverRequest(_RunOptions):
    query: str | None = Field(None, max_length=100)
    limit: int = Field(10, ge=1, le=25)

    @field_validator("query")
    @classmethod
    def _query(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = clean_text(v)
        if v.startswith("-"):
            raise ValueError("query may not start with '-'")
        return v or None


class Progress(BaseModel):
    sources_total: int = 0
    sources_done: int = 0


class Engagement(BaseModel):
    score: float | None = None
    comments: float | None = None
    likes: float | None = None
    shares: float | None = None
    views: float | None = None


class EvidenceItem(BaseModel):
    id: str
    source: str
    kind: EvidenceKind
    url: str
    title: str
    text: str = Field(max_length=2000)
    author: str | None = None
    published_at: str | None = None
    engagement: Engagement
    relevance_score: float = Field(ge=0, le=1)
    cluster_id: str | None = None
    extra: dict[str, Any] = {}


class Cluster(BaseModel):
    id: str
    title: str
    item_ids: list[str]
    sources: list[str]


class Market(BaseModel):
    id: str
    question: str
    url: str
    probability: float | None = None
    volume_usd: float | None = None
    closes_at: str | None = None


class SourceRun(BaseModel):
    source: str
    status: Literal["ok", "empty", "skipped", "failed"]
    item_count: int
    duration_ms: int | None = None
    error: str | None = None


class Window(BaseModel):
    # Derived: to = generated_at, from = to - window_days. Upstream gives only window_days.
    from_: datetime = Field(alias="from")
    to: datetime
    model_config = ConfigDict(populate_by_name=True)


class UpstreamInfo(BaseModel):
    repo: str
    ref: str
    version: str | None = None


class ResearchResult(BaseModel):
    topic: str
    generated_at: datetime
    window: Window
    evidence: list[EvidenceItem]
    clusters: list[Cluster]
    markets: list[Market]
    source_runs: list[SourceRun]
    upstream: UpstreamInfo
    raw_available: bool = True


class DiscoverTopic(BaseModel):
    name: str
    score: float
    momentum: str
    sources: list[str]
    suggested_query: str


class DiscoverResult(BaseModel):
    topics: list[DiscoverTopic]
    source_runs: list[SourceRun]
    upstream: UpstreamInfo


class Job(BaseModel):
    id: str
    kind: JobKind
    status: JobStatus
    request: dict[str, Any]
    cache_hit: bool = False
    client_ref: str | None = None
    progress: Progress = Progress()
    result: ResearchResult | DiscoverResult | None = None
    error: Problem | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    expires_at: datetime | None = None


class JobSummary(Job):
    """Same shape as Job; `result` is always omitted in listings."""


class JobList(BaseModel):
    items: list[JobSummary]
    next_cursor: str | None = None


class VersionInfo(BaseModel):
    api_version: str
    schema_version: str
    upstream_ref: str
    upstream_repo: str


class SourceStatus(BaseModel):
    name: str
    usable: bool
    state: str
    detail: str | None = None


class SourceList(BaseModel):
    sources: list[SourceStatus]
    checked_at: datetime
    cached: bool
