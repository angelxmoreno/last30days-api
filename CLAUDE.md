# CLAUDE.md

Guidance for Claude Code in this repo. Full design lives in `spec.md` — read it first. If code and spec disagree, update the spec (spec section 11 says so).

## What this is

Private HTTP API that wraps the [last30days](https://github.com/mvanhorn/last30days-skill) research CLI. Takes a topic, runs upstream as a subprocess (`--emit=json`), normalizes output, returns scored evidence as JSON. Async jobs, SQLite, FastAPI. Client is a separate Bun/TS app (out of scope).

## Owner context

Owner does **not** know Python. So:
- Write plain, boring, readable Python. No clever metaprogramming.
- Explain Python-specific choices (venv, `uv`, typing, async) briefly when first introduced, in the reply or a short code comment.
- Give exact commands to run things (install, test, start). Never assume Python tooling knowledge.
- Keep the file count and dependency count low. Prefer stdlib.

## Non-negotiables (from spec)

- Upstream is pinned (`UPSTREAM_REF`), never modified, never imported. Subprocess + documented JSON only.
- argv list only, no shell. Topic length-limited, control chars stripped. Process-group kill on cancel/timeout.
- Per-job temp dir with `--save-dir` inside it. Never upstream's default `~/Documents/Last30Days`.
- Never guess values in the normalizer. Missing required field = `schema_mismatch`, keep raw output.
- Errors are `application/problem+json` with the stable `code` list in spec section 4.
- `openapi.yaml` is hand-authored and authoritative; Pydantic models mirror it. CI fails on drift.
- X/Twitter off by default. Credentials to upstream via env only. No secrets in repo.
- Additive changes only within `/v1`.

## Layout and commands

- `app/` service: `config`, `models` (API shapes), `normalizer` (upstream JSON -> models), `runner` (subprocess + env allowlist), `db` (SQLite), `jobs` (queue/cache/cancel/recovery), `webhooks`, `main` (routes), `openapi_schema` (export).
- `tests/` offline; `tests/fixtures/` are real recorded upstream outputs. `scripts/` = upstream bump/check and live smoke.
- `openapi.yaml` is **generated** from the app (`uv run python -m app.openapi_schema`); CI fails on drift. Regenerate after any route or model change.
- Before finishing any change: `uv run ruff check . && uv run ruff format --check . && uv run mypy app scripts && uv run pytest`.
- Upstream facts and field mappings live in `spec.md` section 12.

## Stack

Python 3.12+, FastAPI, Pydantic v2, SQLite, Ruff, mypy, pytest. Docker image targets arm64 (Oracle A1) — check wheels.

## Build order

Follow spec section 10 phases in order. Start with section 11 (read upstream docs, confirm real flags/JSON fields, update spec) before writing code.

## Git

- Repo: https://github.com/angelxmoreno/last30days-api (public).
- Do not stage, commit, or push unless asked.
- In **this repo**, commits **do** carry the trailer (owner's explicit choice, overrides global default):
  `Co-Authored-By: Claude <noreply@anthropic.com>`
- Public repo: never commit secrets, real API keys, or `.env` files.

## Docker scope

**Decision: all Docker files live in this repo.** `Dockerfile`, `.dockerignore`, and a `compose.yaml` for local runs / for client apps to spin the service up. The image is built and health-checked in this repo's CI. Don't create a separate Docker repo. Only revisit if a multi-service stack (this API + client apps) is wanted later.
