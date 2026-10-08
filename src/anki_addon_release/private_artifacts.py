"""Keep recovery artifacts outside every Git checkout, including linked worktrees."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def validate_private_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    parent = path if path.is_dir() else path.parent
    while not parent.exists():
        parent = parent.parent
    result = subprocess.run(
        ["git", "-C", str(parent), "rev-parse", "--git-dir"], capture_output=True
    )
    if result.returncode == 0 or any((p / ".git").exists() for p in (path, *path.parents)):
        raise ValueError("Private artifacts must be outside Git checkouts")
    return path


def private_directory(family: str) -> Path:
    configured = os.environ.get("ANKI_PUBLICATION_PRIVATE_DIR")
    if not configured:
        result = subprocess.run(
            ["git", "config", "--get", "publication.privateDirectory"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            configured = result.stdout.strip()
    if not configured or not Path(configured).expanduser().is_absolute():
        raise ValueError(
            "Set ANKI_PUBLICATION_PRIVATE_DIR to an absolute directory outside Git checkouts"
        )
    directory = validate_private_path(Path(configured) / family)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory
