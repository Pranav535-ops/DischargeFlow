"""Compatibility entrypoint: `uvicorn server:app`, the Dockerfile and vercel.json all point here.

The application lives in the `app` package (see app/main.py).
"""
from app.main import app  # noqa: F401
