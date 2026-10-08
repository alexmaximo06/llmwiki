"""Locations for local-mode derived index and generated artifacts."""
import os
from pathlib import Path


def index_root(workspace_path: str | Path) -> Path:
    configured = os.environ.get("LLMWIKI_INDEX_PATH", "").strip()
    return Path(configured).expanduser().resolve() if configured else Path(workspace_path).resolve() / ".llmwiki"


def index_db_path(workspace_path: str | Path) -> Path:
    return index_root(workspace_path) / "index.db"


def index_cache_root(workspace_path: str | Path) -> Path:
    return index_root(workspace_path) / "cache"
