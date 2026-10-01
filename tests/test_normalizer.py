import copy

import pytest

from app.models import UpstreamInfo
from app.normalizer import SchemaMismatch, normalize_discover, normalize_research
from tests.conftest import fixture_json

UP = UpstreamInfo(repo="r", ref="v3.25.0", version="3.25.0")
RESEARCH = ["research_rich.json", "research_partial.json", "research_empty.json", "research_polymarket.json"]


@pytest.mark.parametrize("name", RESEARCH)
def test_research_fixtures_normalize(name: str) -> None:
    res = normalize_research(fixture_json(name), UP)
    scores = [e.relevance_score for e in res.evidence]
    assert scores == sorted(scores, reverse=True)
    assert len({e.id for e in res.evidence}) == len(res.evidence)
    assert all(len(e.text) <= 2000 for e in res.evidence)
    assert (res.window.to - res.window.from_).days == 30
    for c in res.clusters:
        assert all(i in {e.id for e in res.evidence} for i in c.item_ids)


def test_partial_fixture_reports_failed_source() -> None:
    res = normalize_research(fixture_json("research_partial.json"), UP)
    runs = {r.source: r for r in res.source_runs}
    assert runs["reddit"].status == "failed" and runs["reddit"].error == "rate-limited"
    assert runs["hackernews"].status == "ok" and runs["hackernews"].item_count > 0


def test_empty_fixture() -> None:
    res = normalize_research(fixture_json("research_empty.json"), UP)
    assert res.evidence == [] and {r.status for r in res.source_runs} == {"empty"}


def test_polymarket_becomes_market_and_engagement_extras() -> None:
    res = normalize_research(fixture_json("research_polymarket.json"), UP)
    assert res.markets and res.markets[0].volume_usd is not None
    pm = next(e for e in res.evidence if e.source == "polymarket")
    assert pm.kind == "market" and pm.extra["engagement"]["liquidity"] > 0


def test_engagement_aliases() -> None:
    res = normalize_research(fixture_json("research_rich.json"), UP)
    reddit = next(e for e in res.evidence if e.source == "reddit")
    assert reddit.engagement.score is not None and reddit.engagement.comments is not None
    hn = next(e for e in res.evidence if e.source == "hackernews")
    assert hn.engagement.score is not None


@pytest.mark.parametrize(
    "field",
    ["query", "generated_at", "window_days", "results", "clusters", "source_status", "schema_version"],
)
def test_missing_top_level_field_is_mismatch(field: str) -> None:
    raw = copy.deepcopy(fixture_json("research_rich.json"))
    del raw[field]
    with pytest.raises(SchemaMismatch, match=field):
        normalize_research(raw, UP)


def test_missing_result_field_is_mismatch() -> None:
    raw = copy.deepcopy(fixture_json("research_rich.json"))
    del raw["results"][0]["relevance_score"]
    with pytest.raises(SchemaMismatch, match="relevance_score"):
        normalize_research(raw, UP)


def test_rejects_major_version_and_comparison() -> None:
    raw = copy.deepcopy(fixture_json("research_rich.json"))
    raw["schema_version"] = "2.0"
    with pytest.raises(SchemaMismatch):
        normalize_research(raw, UP)
    with pytest.raises(SchemaMismatch, match="comparison"):
        normalize_research({"schema_version": "1.3", "comparison": True, "reports": []}, UP)


def test_unknown_fields_go_to_extra_and_minor_bump_ok() -> None:
    raw = copy.deepcopy(fixture_json("research_rich.json"))
    raw["schema_version"] = "1.9"
    raw["results"][0]["brand_new"] = {"a": 1}
    res = normalize_research(raw, UP)
    assert any(e.extra.get("brand_new") == {"a": 1} for e in res.evidence)


def test_long_summary_truncated() -> None:
    raw = copy.deepcopy(fixture_json("research_rich.json"))
    raw["results"][0]["summary"] = "x" * 5000
    assert max(len(e.text) for e in normalize_research(raw, UP).evidence) == 2000


def test_discover_fixture_and_limit() -> None:
    res = normalize_discover(fixture_json("discover.json"), UP, limit=3)
    assert 0 < len(res.topics) <= 3
    assert res.topics[0].suggested_query == res.topics[0].name


def test_discover_nothing_solid_and_wrong_kind() -> None:
    raw = copy.deepcopy(fixture_json("discover.json"))
    raw["outcome"] = "nothing-solid"
    assert normalize_discover(raw, UP, 10).topics == []
    raw["kind"] = "research"
    with pytest.raises(SchemaMismatch):
        normalize_discover(raw, UP, 10)
