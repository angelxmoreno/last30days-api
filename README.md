# last30days-api

Private HTTP API around the [last30days](https://github.com/mvanhorn/last30days-skill) research skill. Give it a topic; it runs the upstream CLI as a background job and returns scored evidence (Reddit, Hacker News, Polymarket, GitHub, StockTwits, and more) as JSON.

> **Status: planning.** Design is in [`spec.md`](spec.md). No code yet.

## How it works

```
Client --HTTP--> FastAPI --> SQLite (jobs, results, cache)
                    |
                    v
           worker pool --> upstream CLI subprocess --> JSON --> normalizer --> result
```

- Async jobs: `POST /v1/research` returns `202` + a job; poll `GET /v1/jobs/{id}`.
- Evidence only. No summaries, no LLM calls.
- Upstream is pinned to a release and never modified.
- Static bearer API keys. Not meant for public exposure.

## Stack

Python 3.12+, FastAPI, Pydantic v2, SQLite. One Docker container (arm64 friendly).

## Docker

Docker lives in this repo, not a separate one: `Dockerfile`, `.dockerignore`, and `compose.yaml` (so your other apps can spin the API up and call it). Image is built and health-checked in CI. Files land in phase 1 (skeleton).

## Docs

- [`spec.md`](spec.md) — full design, API contract, build phases
- `openapi.yaml` — API contract for client generation (coming in phase 6)
- [`CLAUDE.md`](CLAUDE.md) — guidance for Claude Code

## Credit

Built with [Claude Code](https://claude.com/claude-code). Research engine: [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill) (MIT).

## License

TBD.
