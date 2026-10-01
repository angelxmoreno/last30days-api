# SPEC: last30days API

> **Status:** phases 1-6 implemented; deployment is out of scope. Deviation from section 5: `openapi.yaml` is generated from the FastAPI app (`python -m app.openapi_schema`) and committed; CI fails on drift and lints it with Redocly. This keeps the file and the code from disagreeing.

Turn the [last30days](https://github.com/mvanhorn/last30days-skill) research skill (MIT, Python) into a private, API-first microservice. Claude Code builds it from this document.

## 1. Goal

A private HTTP service that takes a topic and returns scored evidence from social and web sources (Reddit, Hacker News, Polymarket, GitHub, etc.) as JSON. A separate Bun/TypeScript app (the editorial pipeline, out of scope here) is the client.

**In scope:** wrapper service, async jobs, caching, tests, CI, OpenAPI file, Docker image.
**Out of scope:** LLM calls, claim labeling, script writing, public or multi-user access, changing upstream code.

## 2. Decisions already made

- **Private only.** Static bearer API keys. No public exposure.
- **Wrapper, not fork.** Upstream is pinned and never modified.
- **Python 3.12+ / FastAPI / Pydantic v2 / SQLite.** Ships as one Docker image (must build on arm64 and amd64).
- **Call the upstream CLI as a subprocess with `--emit=json`.** Do not import upstream modules. The CLI and its documented JSON export are the only contract we depend on.
- **Docker in this repo.** `Dockerfile`, `.dockerignore`, and `compose.yaml` (for local use and for client apps to spin the service up) live here, not in a separate repo.
- **Async jobs.** Runs take minutes.
- **Evidence only.** The API returns raw scored evidence, not summaries.

## 3. Architecture

```
Client --HTTP--> FastAPI --> SQLite (jobs, results, cache)
                    |
                    v
           worker pool (bounded) --> upstream CLI subprocess --> JSON --> normalizer --> result
```

- **Pinned upstream:** a release tag or commit SHA in `UPSTREAM_REF`, installed into the image at build time. Follow releases, not `main`.
- **Job states:** `queued`, `running`, `succeeded`, `partial` (some sources failed, result still usable), `failed`, `canceled`.
- **Per-job isolation:** own temp dir, `--save-dir` inside it (never upstream's `~/Documents/Last30Days` default), cleaned up after the job.
- **Safety:** argv list only (no shell), topic length-limited and stripped of control characters, process-group kill on cancel or timeout.
- **Concurrency:** `MAX_CONCURRENT_JOBS` (default 2), queue cap `MAX_QUEUE_DEPTH` (default 20), then `503` with `Retry-After`.
- **Restart recovery:** jobs found `running` at startup become `failed` (`internal_error`).
- **Cache:** key = hash of normalized request + `UPSTREAM_REF` + `schema_version`. Reuse a `succeeded`/`partial` job younger than `max_age_seconds` unless `force_refresh`. Purge jobs after `RETENTION_DAYS` (default 30).
- **Normalizer:** maps upstream JSON to the models below. Unknown fields go in `extra`. Missing required fields fail the job with `schema_mismatch` and keep the raw output for debugging. Never guess values.

## 4. API contract

All `/v1` routes need `Authorization: Bearer <key>`. Errors are `application/problem+json` with a stable `code`: `invalid_request`, `unauthorized`, `not_found`, `rate_limited`, `queue_full`, `conflict`, `engine_timeout`, `engine_failed`, `schema_mismatch`, `canceled`, `internal_error`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness, no auth |
| GET | `/v1/version` | `api_version`, `schema_version`, pinned upstream ref |
| GET | `/v1/sources` | Which sources are usable now (from upstream doctor/diagnose, cached ~10 min, `?refresh=true`) |
| POST | `/v1/research` | Start or reuse a job for a topic. Returns `202` + `Location` + Job |
| POST | `/v1/discover` | Topic-less "what's trending" job, same response |
| GET | `/v1/jobs` | List jobs (filters: `status`, `kind`, `topic`, `client_ref`; cursor pagination; results omitted) |
| GET | `/v1/jobs/{id}` | Job with result when finished |
| DELETE | `/v1/jobs/{id}` | Cancel (no-op if already terminal) |
| GET | `/v1/jobs/{id}/raw` | Unmodified upstream JSON (not covered by compatibility guarantees) |

**Research request:** `topic` (2-300 chars, required), `sources_include[]`, `sources_exclude[]`, `max_age_seconds` (default 21600), `force_refresh`, `timeout_seconds` (30-1800, default 900), `webhook_url`, `client_ref`.

**Discover request:** `query`, `limit` (1-25, default 10), plus the same cache/timeout/webhook/`client_ref` fields.

**Job:** `id`, `kind`, `status`, `request`, `cache_hit`, `client_ref`, `progress {sources_total, sources_done}`, `result`, `error` (problem object), `created_at`, `started_at`, `finished_at`, `expires_at`.

**ResearchResult:** `topic`, `generated_at`, `window {from, to}`, `evidence[]` (sorted by relevance desc), `clusters[]`, `markets[]`, `source_runs[]`, `upstream {repo, ref, version}`, `raw_available`.

**EvidenceItem:** `id` (stable within a job, safe to cite), `source`, `kind` (post, comment, video, transcript_excerpt, market, repo, article, other), `url`, `title`, `text` (excerpt, max 2000 chars), `author`, `published_at`, `engagement {score, comments, likes, shares, views}`, `relevance_score` (0-1 per job), `cluster_id`, `extra`.

**Also:** `Cluster {id, title, item_ids, sources}`, `Market {id, question, url, probability, volume_usd, closes_at}`, `SourceRun {source, status: ok|empty|skipped|failed, item_count, duration_ms, error}`, `DiscoverResult {topics[{name, score, momentum, sources, suggested_query}], source_runs, upstream}`.

**Webhook (optional):** on terminal state, POST the Job JSON with `X-Signature: sha256=<hmac>` (`WEBHOOK_SECRET`), 3 retries with backoff. Polling is authoritative. Block private targets unless `WEBHOOK_ALLOW_PRIVATE=true`.

## 5. OpenAPI deliverable

Claude Code must produce **`openapi.yaml` (OpenAPI 3.1)** from section 4 and commit it at the repo root. It is the contract the Bun side generates its HTTP client from (for example `openapi-typescript` + `openapi-fetch`).

- Hand-authored and authoritative; Pydantic models mirror it.
- CI exports FastAPI's schema and fails on drift from `openapi.yaml`.
- Include `problem+json` error schemas, the bearer security scheme, and `operationId`s suited to client generation (`createResearchJob`, `getJob`, `cancelJob`, `listSources`, etc.).
- Validate it with a linter (Redocly or similar) in CI.
- Additive changes are allowed within `/v1`; breaking changes need `/v2`.

## 6. Sources

Baseline is zero-config: Reddit (with comments), Hacker News, Polymarket, GitHub (works anonymously at a low rate limit; set optional `GITHUB_TOKEN` for headroom, no scopes needed). StockTwits is not in upstream v3.25.0, see section 12. Optional with credentials: Bluesky (free app password), YouTube (yt-dlp), Brave web search (2,000 free queries/month), ScrapeCreators (10,000 free calls). Pass credentials to the engine via env only.

**X/Twitter is disabled by default.** Upstream reads browser cookies, which a server does not have, and cookie-based access is against X's terms.

## 7. Configuration (env)

**Wrapper settings:** `API_KEYS` (required, comma-separated), `DATA_DIR` (`/data`), `MAX_CONCURRENT_JOBS`, `MAX_QUEUE_DEPTH`, `DEFAULT_TIMEOUT_SECONDS`, `RETENTION_DAYS`, `WEBHOOK_SECRET`, `WEBHOOK_ALLOW_PRIVATE`, `SOURCES_EXCLUDE_DEFAULT` (default `x`).

**Upstream passthrough (allowlist).** The engine subprocess never inherits the server environment. The runner builds its env from scratch: `PATH`, a per-job `HOME` and `LAST30DAYS_MEMORY_DIR`, plus only the allowlisted vars below, copied from the server env when set. Everything is optional; a source without its credential reports `skipped-unconfigured` and does not make a job `partial`.

| Group | Vars |
|---|---|
| Credentials | `GITHUB_TOKEN`, `BSKY_HANDLE`, `BSKY_APP_PASSWORD`, `BRAVE_API_KEY`, `SCRAPECREATORS_API_KEY`, `SERPER_API_KEY`, `EXA_API_KEY`, `PERPLEXITY_API_KEY` |
| Reddit tuning | `LAST30DAYS_REDDIT_KEYLESS_RATE`, `LAST30DAYS_REDDIT_BACKEND` |
| Source control | `INCLUDE_SOURCES`, `EXCLUDE_SOURCES` |
| Timeouts and budgets | `LAST30DAYS_ENRICH_BUDGET_SECONDS`, `LAST30DAYS_DOCTOR_PROBE_TIMEOUT`, `LAST30DAYS_YT_SEARCH_TIMEOUT`, `LAST30DAYS_TRANSCRIPT_TIMEOUT` |
| Web search | `LAST30DAYS_SEARXNG_URL` |
| Behavior | `LAST30DAYS_STRICT_EXIT`, `LAST30DAYS_DEBUG` |

**Never passed through** (blocked even if set on the server): `LAST30DAYS_API_KEY` / `LAST30DAYS_API_BASE` (reroute to a remote API, break the agent JSON profile); cookie and X login vars (`FROM_BROWSER`, `AUTH_TOKEN`, `BROWSER_CDP_URL`, `X_BEARER_TOKEN`, `XAI_API_KEY`, `XQUIK_API_KEY`, `TRUTHSOCIAL_TOKEN`); LLM keys (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`; no LLM calls in scope); path and keychain vars (`LAST30DAYS_CONFIG_DIR`, `LAST30DAYS_CORPUS_*`, `LAST30DAYS_SKIP_KEYCHAIN`, `LAST30DAYS_KEYCHAIN_ALIASES`, `LAST30DAYS_PASS_PREFIX`).

The allowlist lives in one constant in code. The repo ships `.env.example` listing every supported var, commented out, with a note on what each unlocks. One test asserts blocked vars never reach the subprocess. Adding a var = add it to the allowlist and `.env.example`.

## 8. Tests and CI

**Every PR (offline, blocking)**
- Lint (Ruff), type check (mypy), unit tests (pytest).
- **Contract tests:** recorded upstream `--emit=json` fixtures (rich run, partial run, empty run, discover run) must normalize cleanly and validate against the models and `openapi.yaml`.
- **Wrapper tests with the engine mocked:** job lifecycle, cache hit/miss/force refresh, cancel, timeout, restart recovery, queue full, auth failures, webhook signing and retry, error shapes.
- **OpenAPI drift check** and spec lint.
- Docker image builds and `/health` responds.

**Upstream bump (weekly + manual)**
- A script finds the latest upstream release, updates `UPSTREAM_REF`, and opens a PR with the changelog excerpt.
- On that PR, CI runs the pinned engine's doctor command, records a tiny real query, diffs the JSON shape (keys and types only) against the previous fixture, and fails on any `schema_mismatch`.
- Green PRs are merged by hand. No auto-merge.

**Live smoke (daily, non-blocking)**
- One tiny real query against zero-config sources; assert non-empty evidence and required fields.
- Reddit may block datacenter IPs, so a Reddit failure is a warning. On failure, open or update one tracking issue; never gate merges on it.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Upstream output changes (1,200+ commits, fast-moving) | Pin, versioned JSON profile, fixtures, bump-PR gate, `schema_mismatch` |
| Reddit blocks datacenter IPs | `partial` status, per-source health, proxy later if needed |
| X cookie and ToS issues | Off by default |
| Long runs exhaust memory or rate limits | Concurrency and queue caps, timeouts, process-group kill |
| Platform terms of service | Private use only; store truncated excerpts, not full content |

## 10. Build phases

1. **Skeleton:** FastAPI app, config, auth, `/health`, `/v1/version`, Dockerfile, CI lint and tests. *Done when* the container starts and auth works.
2. **Runner + normalizer:** subprocess runner, fixture-driven normalizer, `schema_mismatch`. *Done when* fixtures normalize and a real local run yields a valid result.
3. **Jobs:** SQLite store, queue, workers, cancel, cache, recovery, `/v1/research`, `/v1/jobs*`. *Done when* wrapper tests pass.
4. **Sources and discover:** `/v1/sources`, `/v1/discover`.
5. **Webhooks, rate limits, purge.**
6. **OpenAPI file and CI hardening:** `openapi.yaml`, drift check, bump workflow, live smoke.

## 11. First step for Claude Code

Before writing code, read the pinned upstream's `skills/last30days/SKILL.md` and `docs/reference/json-export.md`, and confirm:
- the exact CLI flags for research, discover, and doctor/diagnose;
- the real JSON field names, to replace the assumptions in section 4;
- whether per-source failures appear in the JSON (if not, infer them from stderr or exit info and document the heuristic);
- that every dependency installs on Python 3.12 arm64.

Update this spec with anything that differs before building.
## 12. Confirmed upstream facts (section 11 results)

Checked against upstream **v3.25.0** (latest release, 2026-09-18) by reading `docs/reference/json-export.md`, `SKILL.md`, and the CLI source, then running the real CLI locally on Python 3.12 with an empty `HOME`. Set `UPSTREAM_REF=v3.25.0`. Where this section conflicts with sections 3-7, **this section wins**; fold the changes into those sections when building the matching phase.

### CLI

- Entry point: `python skills/last30days/scripts/last30days.py`. Needs **Python 3.12+**; upstream has **zero Python dependencies** (`dependencies = []`), so arm64 wheels are a non-issue.
- Research: `<topic> --emit=json --save-dir=<job dir>`. `--emit=json` defaults to `--json-profile=agent` (versioned, currently `schema_version` `1.3`). Exit `0` ok, `2` usage/config error, `3` remote-API clarifying question (not used by us).
- Useful research flags: `--search a,b,c` (source allowlist), `--quick` / `--deep`, `--days N` (default 30), `--max-results`, `--max-per-source`, `--no-browser-cookies` (**always pass it**).
- Discover: `--discover [DOMAIN] --emit=json`. Bare `--discover` = global trending (our `/v1/discover`). `--discover-shallow` is fast (~1s, listing evidence only); default runs a full research pass per topic (minutes).
- Doctor: `doctor --json` (positional word `doctor`, also `--diagnose`). Plain `doctor` is text. The JSON has `sources.<name>.audit_state` (`working`, `could-be-on`, ...), per-source `cli` status, `engine_version`. Doctor does a live probe of free sources (~10s each), so cache it (spec already says ~10 min).
- **No timeout flag.** The wrapper enforces timeouts (process-group kill), as designed.
- **No limit flag for discover.** `limit` is applied by the wrapper by slicing `results`.
- Isolation: `--save-dir` also holds a `*-raw.json` copy of the output and (discover) a `research.db`. Set `HOME` and `LAST30DAYS_MEMORY_DIR` to the per-job temp dir too, so nothing touches `~/.config/last30days` or `~/Documents/Last30Days`.
- Progress and diagnostics go to **stderr** (`[HN] Found 15 stories`, `✓ Research complete (...)`). Stdout is clean JSON. Run took ~1.2s for a quick 3-source query.
- Headless runs use a **deterministic planner** (no LLM). Upstream says output is better when a reasoning model passes `--plan`. Spec says no LLM calls, so accept the deterministic path. Revisit only if evidence quality is poor.

### Real JSON shape (agent profile 1.3) vs section 4 assumptions

Top level: `schema_version`, `query`, `generated_at`, `window_days`, `source_status`, `freshness_verdicts`, `clusters`, `results`. All always present.

| Our model | Upstream reality | Normalizer rule |
|---|---|---|
| `window {from,to}` | Only `window_days` + `generated_at` | Derive `to`=`generated_at`, `from`=`to - window_days`. Documented derivation, not a guess. |
| `EvidenceItem.id` | `candidate_id` (a string, often the URL) | Use `candidate_id`; fall back to `ev_<index>` only if absent (it is documented as always present since 1.2). |
| `kind` | Not provided | Map from `source` via a small table; unknown = `other`. |
| `text` | `summary` | Truncate to 2000 chars. |
| `author` | Not provided at top level | `null` unless found in `engagement`/extra. |
| `engagement {score,comments,likes,shares,views}` | Free-form per source (`points`, `comments`, `score`, `num_comments`, `likes`, `reposts`) | Map known names (`points`/`score`->score, `num_comments`/`comments`->comments, `reposts`->shares); everything else to `extra`. |
| `relevance_score` | `relevance_score` 0.0-1.0 | Direct. Sort desc. |
| `cluster_id` | `cluster` = zero-based index into `clusters` | Cluster `id` = `c<index>`. |
| `Cluster {id,title,item_ids,sources}` | `{title, summary, sources, engagement_total}`, no member list | Build `item_ids` from results whose `cluster` index matches. |
| `markets[]` | Not a separate field; Polymarket items appear in `results` with `source: "polymarket"` | Derive `Market` only if the item carries probability/volume fields (check on a real Polymarket fixture); otherwise leave `markets` empty. **Open item for phase 2.** |
| `SourceRun {status ok/empty/skipped/failed, item_count, duration_ms, error}` | `source_status` map only: `ok`, `no-results`, `partial`, `rate-limited`, `auth-failed`, `payment-required`, `unreachable`, `timeout`, `schema-drift`, `skipped-unconfigured`, `error`. No counts, no durations. | Map: `ok`->ok, `no-results`->empty, `skipped-unconfigured`->skipped, `partial`->failed with `error` text = state (job becomes `partial`), all others->failed. `item_count` = count of results with that `source`. `duration_ms` = `null` (not available). Keep the raw upstream state in `error`/`extra`. |
| `upstream {repo,ref,version}` | `engine_version` only via doctor | Fill from `UPSTREAM_REF` + doctor/`--version` check at startup. |
| `raw_available` | n/a | True when the saved JSON exists. |

**Per-source failures DO appear in the JSON** (`source_status`), so no stderr heuristic is needed. Example seen live: `{"hackernews": "ok", "reddit": "rate-limited"}`. Job status rule: any source in a failure state while evidence exists = `partial`; no evidence and all failed = `failed` (`engine_failed`); `no-results` everywhere = `succeeded` with empty evidence. `skipped-unconfigured` does not make a job `partial`.

`/v1/jobs/{id}/raw`: serve the stdout JSON verbatim. Note the saved `*-raw.json` has the same keys as the agent profile, and the true unversioned internal profile needs `--json-profile=raw` (a second run). Do not run it twice. "Raw" = the unmodified stdout of the run.

Comparison queries ("X vs Y") return a different envelope (`comparison: true`, `reports[]`). **Reject or pre-empt them**: our API handles one topic per job. Detect the envelope in the normalizer and fail with `schema_mismatch`. Consider rejecting `vs` topics at validation (decide in phase 2).

### Discover JSON (schema 1.1)

Top level: `schema_version`, `kind: "discovery"`, `domain`, `generated_at`, `window_days`, `source_status`, `feeds`, `results[]`, `warnings[]`, `outcome` (`ok` | `nothing-solid`), `weak_signal`.
Result: `rank`, `topic`, `why_spiking`, `momentum` (`new-this-week`|`building`), `velocity_score`, `sources[]`, `engagement{source:{...}}`, `command`, `evidence_urls[]`, `top_comment`, `corroboration_count`, plus nullable angle/queue fields.

Mapping to our `DiscoverResult.topics[]`: `name`=`topic`, `score`=`velocity_score`, `momentum`=`momentum`, `sources`=`sources`, `suggested_query`=`topic` (the upstream `command` is a slash-command string, do not expose it). `nothing-solid` = `succeeded` with empty `topics`. Request field `query` maps to the optional `DOMAIN` argument. Discover also writes a SQLite file into `--save-dir` (covered by per-job isolation).

### Sources

- Zero-config and confirmed working: **Reddit**, **Hacker News**, **Polymarket**, **GitHub** (upstream reads `GITHUB_TOKEN`, else `gh auth token`, else falls back to anonymous REST with a low rate limit and reduced coverage; we use the env var and do not install `gh`).
- **StockTwits is not a source in v3.25.0.** It does not appear in doctor and is only mentioned in internal code. Drop it from the baseline list unless a later upstream tag adds it. Spec section 6 updated by this note.
- **Reddit rate-limited on the first request from a residential IP** (`reddit: rate-limited`). Expect `partial` often, even before datacenter blocking. This confirms the `partial` status design. Optional ScrapeCreators key backfills Reddit.
- Optional, credentialed: Bluesky (`BSKY_HANDLE`, `BSKY_APP_PASSWORD`), YouTube (needs `yt-dlp`), Brave web search (`BRAVE_API_KEY`), ScrapeCreators (TikTok, Instagram, Reddit backfill, YouTube backstop). Upstream also supports many more (Digg, Techmeme, arXiv, TikTok, Threads, LinkedIn, Perplexity...). We expose only what we decide to support; others stay off.
- **X is off** by default through `SOURCES_EXCLUDE_DEFAULT=x` plus `--no-browser-cookies`. The doctor shows X as `unconfigured` with no credentials, so it stays inert even without the exclude.
- **Source names for `--search`** must be upstream's names (`reddit`, `hackernews`, `polymarket`, `github`, `youtube`, `bluesky`, `web`, ...). `sources_include`/`sources_exclude` validate against a fixed allowlist in our code, built from the doctor output.
- Upstream installs helper CLIs by itself in `setup` (yt-dlp, `npx` packages). **Never run `setup`.** Bake what we want into the image at build time instead.

### Image / platform

- Image needs: Python 3.12, `yt-dlp` (optional YouTube), Node only if we enable `npx`-installed CLIs (we do not; skip Node in v1 and keep the image small). `ffmpeg` is not needed for evidence-only runs.
- Local dev machine has Python 3.9 as `python3`. Use `uv` (already installed) to get 3.12: `uv python install 3.12`.

### Corrections to apply to earlier sections

- Section 3: pin `UPSTREAM_REF=v3.25.0` (tag). Add `HOME` and `LAST30DAYS_MEMORY_DIR` to per-job isolation. Always pass `--no-browser-cookies`.
- Section 4 models: use the mappings above; `duration_ms` and `Market` can be `null`/empty.
- Section 6: remove StockTwits from baseline. GitHub works anonymously; `GITHUB_TOKEN` is optional (no `gh` CLI in the image).
- Section 8 fixtures: record real `--emit=json` output for (a) rich run with all free sources OK, (b) partial run (Reddit rate-limited, seen live), (c) empty run (obscure topic), (d) discover run (`--discover --discover-shallow`). The partial and discover cases were captured during this check and can seed the fixtures.

### Open items for phase 2

1. Find a topic that returns Polymarket items; inspect fields to decide whether `markets[]` is derivable.
2. Decide on `vs`/comparison topics (reject at validation vs `schema_mismatch`).
3. Check `engagement` key names per source (reddit, github, polymarket) and finish the mapping table.
4. Decide whether `partial` upstream state (rare) should map to a `partial` job even with no failed sources.
