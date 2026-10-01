# syntax=docker/dockerfile:1
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

# Pinned upstream engine (ref comes from the UPSTREAM_REF file; never modified, never imported).
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY UPSTREAM_REF /app/UPSTREAM_REF
RUN git clone --depth 1 --branch "$(cat /app/UPSTREAM_REF)" \
      https://github.com/mvanhorn/last30days-skill /opt/upstream \
    && rm -rf /opt/upstream/.git

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# yt-dlp enables the optional YouTube source (pure Python, works on arm64).
RUN uv pip install --python .venv/bin/python yt-dlp
COPY app ./app

ENV PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/data \
    UPSTREAM_DIR=/opt/upstream \
    PYTHONUNBUFFERED=1
RUN useradd --system --uid 10001 --home-dir /data app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
