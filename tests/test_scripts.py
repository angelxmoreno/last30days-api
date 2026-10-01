import json

from scripts.bump_upstream import body_for
from scripts.check_upstream import diff_shapes, shape
from tests.conftest import fixture_json


def test_shape_ignores_values_keeps_types() -> None:
    a = shape({"a": 1, "b": [{"x": "s"}, {"y": 2}], "c": None})
    assert a == {"a": "int", "b": [{"x": "str", "y": "int"}], "c": "null"}


def test_identical_fixture_shape_has_no_diff() -> None:
    s = shape(fixture_json("research_rich.json"))
    assert diff_shapes(s, s) == ([], [])


def test_removed_field_and_type_change_are_breaking_added_is_not() -> None:
    old = shape(fixture_json("research_rich.json"))
    new = json.loads(json.dumps(fixture_json("research_rich.json")))
    del new["window_days"]
    new["results"][0]["new_field"] = 1
    new["results"][0]["relevance_score"] = "high"
    breaking, additive = diff_shapes(old, shape(new))
    assert any("window_days removed" in b for b in breaking)
    assert any("relevance_score" in b for b in breaking)
    assert any("new_field added" in a for a in additive)


def test_pr_body_contains_versions_and_notes() -> None:
    body = body_for("v1", {"tag_name": "v2", "html_url": "http://x", "body": "fixed things"})
    assert "`v1` -> `v2`" in body and "fixed things" in body
