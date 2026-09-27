"""Entry point matching the challenge's `uvicorn bot:app` convention.

Run locally with:
    uvicorn bot:app --host 0.0.0.0 --port 8080

All actual logic lives in app/ — see README.md for the module map.
"""

from app.main import app

__all__ = ["app"]
