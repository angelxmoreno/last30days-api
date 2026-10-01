"""Live smoke test against a running API: one tiny real query on zero-config sources.

    uv run python -m scripts.smoke http://localhost:8000 <api-key>

Exit 1 on a hard failure. A Reddit failure is only a warning (datacenter IPs get blocked).
"""

import sys
import time

import httpx

REQUIRED = ("id", "source", "kind", "url", "title", "text", "relevance_score", "engagement")


def main(base: str, key: str) -> int:
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(base_url=base, headers=headers, timeout=30) as c:
        body = {
            "topic": "python programming",
            "sources_include": ["hackernews", "reddit", "github"],
            "timeout_seconds": 600,
            "force_refresh": True,
        }
        job = c.post("/v1/research", json=body).raise_for_status().json()
        deadline = time.time() + 660
        while job["status"] in ("queued", "running") and time.time() < deadline:
            time.sleep(5)
            job = c.get(f"/v1/jobs/{job['id']}").raise_for_status().json()
    if job["status"] not in ("succeeded", "partial"):
        print(f"FAIL: job ended {job['status']}: {job.get('error')}")
        return 1
    result = job["result"]
    evidence = result["evidence"]
    runs = {r["source"]: r for r in result["source_runs"]}
    if "reddit" in runs and runs["reddit"]["status"] == "failed":
        print(f"warning: reddit failed ({runs['reddit']['error']}); datacenter blocks are expected")
    if not evidence:
        print("FAIL: no evidence returned")
        return 1
    for item in evidence:
        missing = [f for f in REQUIRED if f not in item]
        if missing:
            print(f"FAIL: evidence item missing {missing}")
            return 1
    failed = [s for s, r in runs.items() if r["status"] == "failed" and s != "reddit"]
    if failed:
        print(f"FAIL: non-reddit sources failed: {failed}")
        return 1
    print(f"ok: {len(evidence)} evidence items, sources {sorted(runs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
