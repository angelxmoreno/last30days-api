"""Map upstream agent-profile JSON (schema 1.x) to our models.

Rules: never guess. A missing required field raises SchemaMismatch. Unknown fields go to `extra`.
"""

from datetime import datetime, timedelta
from typing import Any

from app.models import (
    Cluster,
    DiscoverResult,
    DiscoverTopic,
    Engagement,
    EvidenceItem,
    Market,
    ResearchResult,
    SourceRun,
    UpstreamInfo,
    Window,
)

SUPPORTED_MAJOR = "1"
TEXT_MAX = 2000

KIND_BY_SOURCE = {
    "reddit": "post",
    "hackernews": "post",
    "bluesky": "post",
    "tiktok": "video",
    "instagram": "post",
    "youtube": "video",
    "polymarket": "market",
    "github": "repo",
    "web": "article",
    "grounding": "article",
}
_ENGAGEMENT_ALIASES = {
    "score": "score",
    "points": "score",
    "comments": "comments",
    "num_comments": "comments",
    "likes": "likes",
    "shares": "shares",
    "reposts": "shares",
    "views": "views",
}
_KNOWN_RESULT_KEYS = {
    "candidate_id",
    "title",
    "source",
    "url",
    "published_at",
    "summary",
    "engagement",
    "relevance_score",
    "cluster",
}
_STATUS_MAP = {"ok": "ok", "no-results": "empty", "skipped-unconfigured": "skipped"}


class SchemaMismatch(Exception):
    pass


def _need(d: dict[str, Any], key: str, types: Any, where: str) -> Any:
    if key not in d:
        raise SchemaMismatch(f"missing required field '{where}{key}'")
    if not isinstance(d[key], types) or isinstance(d[key], bool) and types is not bool:
        raise SchemaMismatch(f"field '{where}{key}' has unexpected type {type(d[key]).__name__}")
    return d[key]


def _check_version(raw: dict[str, Any]) -> None:
    ver = _need(raw, "schema_version", str, "")
    if ver.split(".")[0] != SUPPORTED_MAJOR:
        raise SchemaMismatch(f"unsupported upstream schema_version {ver!r}")
    if raw.get("comparison"):
        raise SchemaMismatch("comparison envelope is not supported")


def _ts(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaMismatch(f"field '{field}' is not an RFC 3339 timestamp") from exc


def _source_runs(raw: dict[str, Any], counts: dict[str, int]) -> list[SourceRun]:
    status = _need(raw, "source_status", dict, "")
    runs = []
    for name in sorted(status):
        state = status[name]
        if not isinstance(state, str):
            raise SchemaMismatch(f"source_status.{name} is not a string")
        mapped = _STATUS_MAP.get(state, "failed")
        runs.append(
            SourceRun(
                source=name,
                status=mapped,
                item_count=counts.get(name, 0),
                error=state if mapped == "failed" else None,
            )
        )
    return runs


def _engagement(raw_eng: Any, extra: dict[str, Any]) -> Engagement:
    if not isinstance(raw_eng, dict):
        raise SchemaMismatch("result.engagement is not an object")
    fields: dict[str, float] = {}
    leftovers: dict[str, Any] = {}
    for k, v in raw_eng.items():
        target = _ENGAGEMENT_ALIASES.get(k)
        if target and isinstance(v, int | float) and not isinstance(v, bool) and target not in fields:
            fields[target] = v
        else:
            leftovers[k] = v
    if leftovers:
        extra["engagement"] = leftovers
    return Engagement(**fields)


def normalize_research(raw: Any, upstream: UpstreamInfo, raw_available: bool = True) -> ResearchResult:
    if not isinstance(raw, dict):
        raise SchemaMismatch("top-level JSON is not an object")
    _check_version(raw)
    topic = _need(raw, "query", str, "")
    generated = _ts(_need(raw, "generated_at", str, ""), "generated_at")
    days = _need(raw, "window_days", int, "")
    clusters_raw = _need(raw, "clusters", list, "")
    results_raw = _need(raw, "results", list, "")

    evidence: list[EvidenceItem] = []
    markets: list[Market] = []
    for i, r in enumerate(results_raw):
        if not isinstance(r, dict):
            raise SchemaMismatch(f"results[{i}] is not an object")
        w = f"results[{i}]."
        source = _need(r, "source", str, w)
        extra: dict[str, Any] = {k: v for k, v in r.items() if k not in _KNOWN_RESULT_KEYS}
        eng_raw = r.get("engagement", {})
        item = EvidenceItem(
            id=_need(r, "candidate_id", str, w),
            source=source,
            kind=KIND_BY_SOURCE.get(source, "other"),
            url=r.get("url", ""),
            title=_need(r, "title", str, w),
            text=str(r.get("summary", ""))[:TEXT_MAX],
            published_at=r.get("published_at"),
            engagement=_engagement(eng_raw, extra),
            relevance_score=_need(r, "relevance_score", int | float, w),
            cluster_id=f"c{r['cluster']}" if isinstance(r.get("cluster"), int) else None,
            extra=extra,
        )
        evidence.append(item)
        if source == "polymarket":
            volume = eng_raw.get("volume") if isinstance(eng_raw, dict) else None
            markets.append(
                Market(
                    id=item.id,
                    question=item.title,
                    url=item.url,
                    volume_usd=volume if isinstance(volume, int | float) else None,
                )
            )
    evidence.sort(key=lambda e: e.relevance_score, reverse=True)

    clusters: list[Cluster] = []
    for i, c in enumerate(clusters_raw):
        if not isinstance(c, dict):
            raise SchemaMismatch(f"clusters[{i}] is not an object")
        cid = f"c{i}"
        clusters.append(
            Cluster(
                id=cid,
                title=_need(c, "title", str, f"clusters[{i}]."),
                item_ids=[e.id for e in evidence if e.cluster_id == cid],
                sources=_need(c, "sources", list, f"clusters[{i}]."),
            )
        )
    for e in evidence:
        if e.cluster_id and int(e.cluster_id[1:]) >= len(clusters):
            raise SchemaMismatch(f"result {e.id!r} points at missing cluster {e.cluster_id}")

    counts: dict[str, int] = {}
    for e in evidence:
        counts[e.source] = counts.get(e.source, 0) + 1
    return ResearchResult(
        topic=topic,
        generated_at=generated,
        window=Window(**{"from": generated - timedelta(days=days), "to": generated}),
        evidence=evidence,
        clusters=clusters,
        markets=markets,
        source_runs=_source_runs(raw, counts),
        upstream=upstream,
        raw_available=raw_available,
    )


def normalize_discover(raw: Any, upstream: UpstreamInfo, limit: int) -> DiscoverResult:
    if not isinstance(raw, dict):
        raise SchemaMismatch("top-level JSON is not an object")
    _check_version(raw)
    if raw.get("kind") != "discovery":
        raise SchemaMismatch("expected kind 'discovery'")
    _need(raw, "generated_at", str, "")
    outcome = _need(raw, "outcome", str, "")
    results = _need(raw, "results", list, "")
    topics: list[DiscoverTopic] = []
    counts: dict[str, int] = {}
    for i, r in enumerate(results):
        if not isinstance(r, dict):
            raise SchemaMismatch(f"results[{i}] is not an object")
        w = f"results[{i}]."
        name = _need(r, "topic", str, w)
        srcs = _need(r, "sources", list, w)
        for s in srcs:
            counts[s] = counts.get(s, 0) + 1
        topics.append(
            DiscoverTopic(
                name=name,
                score=_need(r, "velocity_score", int | float, w),
                momentum=_need(r, "momentum", str, w),
                sources=srcs,
                suggested_query=name,
            )
        )
    if outcome == "nothing-solid":
        topics = []
    return DiscoverResult(
        topics=topics[:limit],
        source_runs=_source_runs(raw, counts),
        upstream=upstream,
    )


def failure_summary(runs: list[SourceRun]) -> tuple[bool, bool]:
    """Return (any_failed, any_usable_source)."""
    failed = any(r.status == "failed" for r in runs)
    usable = any(r.status in ("ok", "empty") for r in runs)
    return failed, usable
