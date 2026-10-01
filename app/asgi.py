"""Uvicorn entry point: `uvicorn app.asgi:app` (settings come from the environment)."""

from app.main import create_app

app = create_app()
