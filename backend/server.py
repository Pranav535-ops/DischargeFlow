"""Compatibility entrypoint: `uvicorn server:app`, the Dockerfile and vercel.json all point here.

The application lives in the `app` package (see app/main.py).
"""
import sys
from pathlib import Path

# Vercel imports this file by path from the repository root, so backend/ is not on sys.path there.
BACKEND_DIR = str(Path(__file__).resolve().parent)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.main import app  # noqa: E402,F401
