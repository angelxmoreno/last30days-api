# last30days-api

Private HTTP API around the [last30days](https://github.com/mvanhorn/last30days-skill) research engine. Send a topic, get back scored evidence (Reddit, Hacker News, Polymarket, GitHub, and optional sources) as JSON. Runs are async jobs: you start one, then poll for the result.

Source code, full docs and OpenAPI contract: **https://github.com/angelxmoreno/last30days-api**

## Quick start

```bash
docker run -d --name last30days-api \
  -e API_KEYS=$(openssl rand -hex 32) \
  -v last30days-data:/data \
  -p 8000:8000 \
  angelxmoreno/last30days-api:1
```

Use your own key instead of the generated one so you can call the API:

```bash
KEY=your-key
curl -XPOST localhost:8000/v1/research \
  -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"topic": "rust async runtimes"}'          # 202 + a job id

curl localhost:8000/v1/jobs/<id> -H "Authorization: Bearer $KEY"
```

Health check (no auth): `curl localhost:8000/health`.

## Tags

| Tag | Meaning |
|---|---|
| `1.2.3` | Exact release |
| `1.2`, `1` | Newest release in that line |
| `latest` | Newest release |
| `edge` | Latest commit on `main` (may be unreleased) |

Images are multi-arch (`linux/amd64`, `linux/arm64`). The upstream research engine is pinned and baked into each image.

## Docker Compose

```yaml
services:
  last30days-api:
    image: angelxmoreno/last30days-api:1
    environment:
      API_KEYS: ${LAST30DAYS_API_KEYS}
    volumes:
      - last30days-data:/data
    ports:
      - "8000:8000"
volumes:
  last30days-data:
```

Other containers in the same compose project reach it at `http://last30days-api:8000`.

## Configuration

Environment variables. Only `API_KEYS` is required.

| Variable | Default | Meaning |
|---|---|---|
| `API_KEYS` | required | Comma-separated bearer keys |
| `MAX_CONCURRENT_JOBS` | 2 | Parallel research runs |
| `MAX_QUEUE_DEPTH` | 20 | Waiting jobs before `503` + `Retry-After` |
| `DEFAULT_TIMEOUT_SECONDS` | 900 | Used when a request omits `timeout_seconds` |
| `RETENTION_DAYS` | 30 | Jobs and stored output are purged after this |
| `RATE_LIMIT_PER_MINUTE` | 120 | Per API key |
| `WEBHOOK_SECRET`, `WEBHOOK_ALLOW_PRIVATE` | unset, false | Needed for signed webhooks |
| `SOURCES_EXCLUDE_DEFAULT` | `x` | Sources always off (X stays off) |
| `GITHUB_TOKEN` | unset | Optional, raises the GitHub source rate limit |

More optional source credentials (`BSKY_*`, `BRAVE_API_KEY`, `SCRAPECREATORS_API_KEY`, ...) are listed in the repo's `.env.example`.

## Data and ports

- `/data` (volume): job history, cached results, raw output. Mount a volume or it is lost with the container.
- `8000`: HTTP.
- Runs as a non-root user. The image defines a health check on `/health`.

## License

MIT
