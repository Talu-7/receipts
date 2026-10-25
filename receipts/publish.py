"""`python -m receipts bundle --to FOLDER`: copy everything that is safe to publish into a clean folder."""
from __future__ import annotations

import shutil
from pathlib import Path

from .safety import leakcheck

MARKER = ".receipts-publish-folder"
EXCLUDE_DIRS = {".git", "__pycache__", "out", "node_modules", "submission", ".venv", "venv"}
EXCLUDE_FILES = {".env", "SUBMISSION_GUIDE.md", MARKER}
EXCLUDE_SUFFIXES = {".pyc", ".zip", ".log"}


def bundle(src: str, dst: str) -> tuple[int, list[str]]:
    s, d = Path(src).resolve(), Path(dst).expanduser().resolve()
    if not (s / "receipts" / "__init__.py").exists():
        raise SystemExit("run this from the project folder (the one that contains the 'receipts' folder)")
    if not (s / "docs" / "index.html").exists():
        raise SystemExit("docs/index.html not found: run `python3 -m receipts site` first")
    if d == s or s in d.parents or d in s.parents:
        raise SystemExit("choose a folder OUTSIDE the project (for example a new folder on your Desktop)")
    if d.exists() and any(d.iterdir()):
        if not (d / MARKER).exists():
            raise SystemExit(f"{d} already has files in it. Choose a new, empty folder name.")
        shutil.rmtree(d)  # a previous bundle made by this tool: safe to rebuild
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    for path in s.rglob("*"):
        rel = path.relative_to(s)
        if any(part in EXCLUDE_DIRS for part in rel.parts) or not path.is_file():
            continue
        if path.name in EXCLUDE_FILES or path.suffix in EXCLUDE_SUFFIXES or (path.name.startswith(".env") and path.name != ".env.example"):
            continue
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        n += 1
    (d / MARKER).write_text("created by `python -m receipts bundle`; safe to delete and rebuild\n", encoding="utf-8")
    return n, leakcheck(str(d), [str(s / ".env")])
