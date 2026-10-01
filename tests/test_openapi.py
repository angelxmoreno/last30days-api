from app.openapi_schema import SPEC_PATH, build, render


def test_openapi_file_matches_app() -> None:
    """Fails when routes/models change without regenerating openapi.yaml."""
    assert SPEC_PATH.read_text() == render(build()), "run: uv run python -m app.openapi_schema"


def test_contract_shape() -> None:
    spec = build()
    assert spec["openapi"].startswith("3.1")
    ops = {op["operationId"] for p in spec["paths"].values() for op in p.values()}
    assert {
        "createResearchJob",
        "createDiscoverJob",
        "getJob",
        "cancelJob",
        "listSources",
        "listJobs",
        "getJobRaw",
        "getVersion",
        "getHealth",
    } <= ops
    assert "HTTPBearer" in spec["components"]["securitySchemes"]
    assert "HTTPValidationError" not in spec["components"]["schemas"]
    for path in spec["paths"].values():
        for op in path.values():
            for code, resp in op["responses"].items():
                if int(code) >= 400:
                    assert list(resp["content"]) == ["application/problem+json"]
