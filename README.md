# last30days-api

Private HTTP API around the [last30days](https://github.com/mvanhorn/last30days-skill) research engine. Give it a topic; it runs the upstream CLI as a background job and returns scored evidence (Reddit, Hacker News, Polymarket, GitHub, and optional sources) as JSON.

- **Async jobs.** `POST /v1/research` returns `202` + a job; poll `GET /v1/jobs/{id}`.
- **Evidence only.** No summaries, no LLM calls.
- **Wrapper, not a fork.** Upstream is pinned (`UPSTREAM_REF`), run as a subprocess, never modified or imported.
- **Private.** Static bearer API keys. Do not expose it publicly.

```
Client --HTTP--> FastAPI --> SQLite (jobs, results, cache)
                    |
                    v
           worker pool --> upstream CLI subprocess --> JSON --> normalizer --> result
```

## Quick start

Needs [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you) and Docker.

```bash
cp .env.example .env          # set API_KEYS=some-long-random-string
docker compose up --build     # API on http://localhost:8000
```

```bash
KEY=some-long-random-string
curl -s -XPOST localhost:8000/v1/research \
  -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"topic": "rust async runtimes"}'
# -> 202, {"id": "…", "status": "queued", …}

curl -s localhost:8000/v1/jobs/<id> -H "Authorization: Bearer $KEY"
# -> status: queued | running | succeeded | partial | failed | canceled; result when finished
```

A run takes seconds to a few minutes. `partial` means some sources failed (Reddit often rate-limits) but the result is usable. The same request inside `max_age_seconds` (default 6h) is served from cache; set `force_refresh` to skip it.

## API

Contract: [`openapi.yaml`](openapi.yaml) (OpenAPI 3.1; generate a typed client from it, e.g. `openapi-typescript` + `openapi-fetch` for Bun).

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness, no auth |
| GET | `/v1/version` | API, schema and pinned upstream version |
| GET | `/v1/sources` | Which sources are usable now (cached ~10 min, `?refresh=true`) |
| POST | `/v1/research` | Start or reuse a research job |
| POST | `/v1/discover` | Topic-less "what's trending" job |
| GET | `/v1/jobs` | List jobs (filters, cursor pagination) |
| GET | `/v1/jobs/{id}` | Job, with result when finished |
| DELETE | `/v1/jobs/{id}` | Cancel (no-op if already finished) |
| GET | `/v1/jobs/{id}/raw` | Unmodified upstream JSON (no compatibility guarantee) |

Errors are `application/problem+json` with a stable `code` (`invalid_request`, `unauthorized`, `not_found`, `rate_limited`, `queue_full`, `engine_timeout`, `engine_failed`, `schema_mismatch`, …). Optional webhooks (`webhook_url`) get the final Job JSON with an `X-Signature: sha256=<hmac>` header; polling stays authoritative.

## Configuration

All via environment variables. See [`.env.example`](.env.example) for the full list.

| Var | Default | Meaning |
|---|---|---|
| `API_KEYS` | required | Comma-separated bearer keys |
| `DATA_DIR` | `/data` | SQLite DB and stored raw output |
| `MAX_CONCURRENT_JOBS` / `MAX_QUEUE_DEPTH` | 2 / 20 | Worker pool and queue cap (then `503` + `Retry-After`) |
| `DEFAULT_TIMEOUT_SECONDS` | 900 | Used when a request omits `timeout_seconds` (30-1800) |
| `RETENTION_DAYS` | 30 | Jobs and raw output are purged after this |
| `WEBHOOK_SECRET`, `WEBHOOK_ALLOW_PRIVATE` | unset, false | Needed for webhooks; private targets blocked unless `true` |
| `SOURCES_EXCLUDE_DEFAULT` | `x` | Sources always off. X stays off (cookie-based access breaks its terms) |
| `RATE_LIMIT_PER_MINUTE` | 120 | Per API key |

Optional upstream credentials (`GITHUB_TOKEN`, `BSKY_*`, `BRAVE_API_KEY`, `SCRAPECREATORS_API_KEY`, …) are passed through an allowlist; the engine never inherits the server environment. Details in [`spec.md`](spec.md) section 7.

## Development

```bash
uv sync                                    # install deps into .venv (Python 3.12)
uv run pytest                              # offline tests: engine is mocked, fixtures are real recorded output
uv run ruff check . && uv run ruff format --check .
uv run mypy app scripts
uv run python -m app.openapi_schema        # regenerate openapi.yaml after changing routes or models
```

Run the API without Docker (needs a local clone of the pinned upstream):

```bash
git clone --depth 1 --branch "$(cat UPSTREAM_REF)" https://github.com/mvanhorn/last30days-skill upstream
API_KEYS=dev DATA_DIR=./data UPSTREAM_DIR=./upstream uv run uvicorn app.asgi:app --reload
```

## Docker

Docker lives in this repo: `Dockerfile`, `.dockerignore`, `compose.yaml`. The image clones the pinned upstream at build time, installs `yt-dlp` (optional YouTube source), runs as a non-root user, and keeps state in the `/data` volume.

### Run with Docker Compose

Needs Docker with the Compose plugin (`docker compose version`).

1. **Create your env file.** `compose.yaml` reads `.env`, and Compose refuses to start without it.
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and set `API_KEYS` to one or more long random strings (comma-separated). Generate one with `openssl rand -hex 32`. Add optional credentials like `GITHUB_TOKEN` here too.
2. **Start it.**
   ```bash
   docker compose up --build -d     # first build clones the pinned upstream, takes a minute
   ```
3. **Check it.**
   ```bash
   docker compose ps                          # STATUS should become "healthy"
   curl localhost:8000/health                 # {"status":"ok"}
   curl -H "Authorization: Bearer <a key from API_KEYS>" localhost:8000/v1/version
   ```
4. **Day to day.**
   ```bash
   docker compose logs -f last30days-api      # watch logs
   docker compose restart last30days-api      # after editing .env
   docker compose up --build -d               # after pulling new code or bumping UPSTREAM_REF
   docker compose down                        # stop; job data is kept in the named volume
   docker compose down -v                     # stop AND delete all job data
   ```

Job history, cached results and raw output live in the `last30days-data` volume, so they survive restarts and rebuilds. To change the host port, edit the `ports` line in `compose.yaml` (for example `"9000:8000"`).

### Use it from another project's compose file

Option A: include this service (Compose 2.20+). Services in one compose project share a network, so your app reaches the API by service name.

```yaml
# your-app/compose.yaml
include:
  - path: ../last30days-api/compose.yaml   # adjust to where you cloned this repo

services:
  your-app:
    build: .
    environment:
      LAST30DAYS_API_URL: http://last30days-api:8000
      LAST30DAYS_API_KEY: ${LAST30DAYS_API_KEY}   # one of the keys in this repo's .env
    depends_on:
      last30days-api:
        condition: service_healthy
```

Note that `include` resolves the `.env` and build context relative to this repo's compose file, so run `cp .env.example .env` here first.

Option B: run this stack on its own (steps above) and call it over the published port: `http://localhost:8000` from the host, or `http://host.docker.internal:8000` from inside another container (Docker Desktop).

## CI

- **CI** (every PR): Ruff, mypy, pytest, OpenAPI drift check, Redocly lint, Docker build + `/health`.
- **Upstream bump** (weekly + manual): opens a PR bumping `UPSTREAM_REF` with release notes. Never auto-merged.
- **Upstream check** (on bump PRs): upstream doctor, one tiny real query, JSON shape diff against the fixture, normalizer run.
- **Live smoke** (daily, non-blocking): one tiny real query through the built container; failures open or update one tracking issue.

## Docs

- [`spec.md`](spec.md) — design, API contract, confirmed upstream facts (section 12)
- [`openapi.yaml`](openapi.yaml) — generated API contract
- [`CLAUDE.md`](CLAUDE.md) — guidance for Claude Code

## Credit

Built with [Claude Code](https://claude.com/claude-code). Research engine: [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill) (MIT).

## License

[MIT](LICENSE)
