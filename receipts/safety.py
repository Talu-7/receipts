"""Pre-publish safety check: make sure no secrets are about to be uploaded."""
from __future__ import annotations

import os
import re
from pathlib import Path

SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD)", re.I)
PATTERNS = {
    "Tavily-style API key": re.compile(r"tvly-[A-Za-z0-9_\-]{16,}"),
    "JWT-style token (Nebius/other access token)": re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
SKIP_DIRS = {".git", "__pycache__", "node_modules"}


def known_secret_values(env_files: list[str]) -> list[str]:
    vals: dict[str, str] = {}
    for k, v in os.environ.items():
        if SECRET_NAME.search(k) and len(v) >= 8 and v != "proxy-injected":
            vals[k] = v
    for f in env_files:
        p = Path(f)
        if not p.is_file():
            continue
        for line in p.read_text(errors="ignore").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if SECRET_NAME.search(k) and len(v) >= 8:
                    vals[k.strip()] = v
    return list(vals.values())


def leakcheck(folder: str, env_files: list[str] | None = None) -> list[str]:
    """Return human-readable problems (never the secret itself). Empty list = safe to publish."""
    root = Path(folder)
    secrets = known_secret_values(env_files or [".env", str(root / ".env"), str(root.parent / ".env")])
    problems: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root)
            if name == ".env" or (name.startswith(".env") and name != ".env.example"):
                problems.append(f"{rel}: this file holds your keys. Do NOT upload it (delete it from this folder).")
                continue
            try:
                if path.stat().st_size > 5_000_000:
                    continue
                text = path.read_bytes().decode("utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for s in secrets:
                if s in text:
                    problems.append(f"{rel}: contains one of your real API keys/tokens.")
                    break
            for label, pat in PATTERNS.items():
                if pat.search(text):
                    problems.append(f"{rel}: looks like it contains a {label}.")
    return problems
