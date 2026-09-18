from __future__ import annotations

import asyncio
import os
from pathlib import Path

import uvicorn

from .app import create_app

ROOT = Path(__file__).resolve().parent.parent
# Keep large runtime/model caches beside the project, regardless of whether the
# project lives on C:, D:, E: or another local drive.
(ROOT / ".cache" / "pip").mkdir(parents=True, exist_ok=True)
(ROOT / ".cache" / "huggingface").mkdir(parents=True, exist_ok=True)
os.environ.setdefault("PIP_CACHE_DIR", str(ROOT / ".cache" / "pip"))
os.environ.setdefault("HF_HOME", str(ROOT / ".cache" / "huggingface"))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(ROOT / ".cache" / "huggingface" / "hub"))
app = create_app(ROOT)

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8787,
        log_level="info",
    )
