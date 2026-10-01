"""Check a pinned upstream checkout still produces JSON we can normalize.

Used by CI on upstream-bump PRs:
  - runs the engine's `doctor --json`
  - records one tiny real query
  - diffs the JSON *shape* (keys and types only) against tests/fixtures/research_rich.json
  - normalizes it; any SchemaMismatch fails the check

    UPSTREAM_DIR=./upstream uv run python -m scripts.check_upstream [--out recorded.json]
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from app import runner
from app.config import Settings, load_settings
from app.models import UpstreamInfo
from app.normalizer import SchemaMismatch, normalize_research

FIXTURE = Path(__file__).resolve().parent.parent / "tests/fixtures/research_rich.json"
TOPIC = "python programming"
SOURCES = ["hackernews", "polymarket", "github"]


# Maps keyed by source name / counter name: their keys vary by run, so only value types count.
FREE_MAPS = {"source_status", "engagement"}


def _tname(x: Any) -> str:
    return "number" if isinstance(x, int | float) and not isinstance(x, bool) else type(x).__name__


def shape(value: Any) -> Any:
    """Reduce JSON to keys and type names. Lists merge the shapes of all their elements."""
    if isinstance(value, dict):
        return {
            k: (
                {"*": sorted({_tname(x) for x in v.values()})}
                if k in FREE_MAPS and isinstance(v, dict)
                else shape(v)
            )
            for k, v in sorted(value.items())
        }
    if isinstance(value, list):
        merged: dict[str, Any] = {}
        types: set[str] = set()
        for item in value:
            s = shape(item)
            if isinstance(s, dict):
                merged = _merge(merged, s)
            else:
                types.add(s)
        return [merged] if merged else sorted(types)
    return type(value).__name__ if value is not None else "null"


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        elif k in out and out[k] != v:
            out[k] = sorted({*_as_list(out[k]), *_as_list(v)}, key=str)
        else:
            out[k] = v
    return out


def _as_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else [v]


def diff_shapes(old: Any, new: Any, path: str = "$") -> tuple[list[str], list[str]]:
    """Return (breaking, additive). Removed keys and changed types are breaking."""
    breaking: list[str] = []
    additive: list[str] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for k in old:
            if k not in new:
                # Keys that only appear on some sources (e.g. engagement counters) can vanish
                # from a tiny run; only top-level and result-level keys are strict.
                (breaking if path.count(".") < 2 or path.endswith("]") else additive).append(
                    f"{path}.{k} removed"
                )
            else:
                b, a = diff_shapes(old[k], new[k], f"{path}.{k}")
                breaking += b
                additive += a
        additive += [f"{path}.{k} added" for k in new if k not in old]
    elif isinstance(old, list) and isinstance(new, list):
        if old and new and isinstance(old[0], dict) and isinstance(new[0], dict):
            return diff_shapes(old[0], new[0], f"{path}[]")
        if old and new and old != new and not (isinstance(old[0], dict) or isinstance(new[0], dict)):
            breaking.append(f"{path} element types {old} -> {new}")
    elif old != new:
        breaking.append(f"{path} type {old} -> {new}")
    return breaking, additive


async def record(settings: Settings) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        job_dir = Path(tmp)
        (job_dir / "home").mkdir()
        (job_dir / "out").mkdir()
        env = runner.build_env(dict(os.environ), job_dir, ["x"])
        doctor = await runner.run_subprocess(runner.doctor_argv(settings, job_dir), env, 180)
        if doctor.returncode != 0 or "sources" not in json.loads(doctor.stdout):
            raise SystemExit(f"doctor failed: {doctor.stderr[-500:]}")
        argv = runner.research_argv(settings, TOPIC, SOURCES, job_dir)
        argv.insert(argv.index("--"), "--quick")
        res = await runner.run_subprocess(argv, env, 600)
        if res.returncode != 0:
            raise SystemExit(f"engine exited {res.returncode}: {res.stderr[-500:]}")
        parsed: dict[str, Any] = json.loads(res.stdout)
        return parsed


def main() -> int:
    settings = load_settings({"API_KEYS": "x", **{k: v for k, v in os.environ.items()}})
    recorded = asyncio.run(record(settings))
    if "--out" in sys.argv:
        Path(sys.argv[sys.argv.index("--out") + 1]).write_text(json.dumps(recorded, indent=1))
    try:
        normalize_research(recorded, UpstreamInfo(repo="", ref=settings.upstream_ref))
    except SchemaMismatch as exc:
        print(f"FAIL schema_mismatch: {exc}")
        return 1
    breaking, additive = diff_shapes(shape(json.loads(FIXTURE.read_text())), shape(recorded))
    for line in additive:
        print(f"note (additive): {line}")
    for line in breaking:
        print(f"BREAKING: {line}")
    print("shape check:", "FAILED" if breaking else "ok")
    return 1 if breaking else 0


if __name__ == "__main__":
    raise SystemExit(main())
