"""Service settings, read from environment variables. Nothing secret has a hard-coded default."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ServiceSettings:
    database_url: str = "sqlite:///service/data/service.db"
    data_dir: str = "service/data"
    public_base_url: str = "http://localhost:8000"
    # HMAC key for session cookies. A random per-process key is used if unset (sessions then die on restart).
    secret_key: str = field(default_factory=lambda: secrets.token_hex(32))
    secret_key_from_env: bool = False
    dev_mode: bool = True
    allow_demo_inputs: bool = True
    # Ed25519 signing key: base64 raw 32-byte seed or a PEM, or a path to a PEM file.
    signing_key: str = ""
    signing_key_file: str = ""
    # Stripe: TEST MODE ONLY. Keys come from the environment and are checked to be test keys.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_lab: str = ""
    # Worker
    runner: str = "docker"
    executor: str = "process"  # "process" (killable child per check) or "inline" (tests/dev only)
    pipeline_factory: str = "service.worker:default_pipeline_factory"
    poll_seconds: float = 2.0
    heartbeat_stale_s: int = 300

    @classmethod
    def from_env(cls) -> "ServiceSettings":
        e = os.environ.get
        data_dir = e("SERVICE_DATA_DIR", "service/data")
        sk = e("SERVICE_SECRET_KEY", "")
        s = cls(
            database_url=e("SERVICE_DATABASE_URL", "") or e("DATABASE_URL", "") or f"sqlite:///{data_dir}/service.db",
            data_dir=data_dir,
            public_base_url=e("SERVICE_PUBLIC_URL", "http://localhost:8000").rstrip("/"),
            dev_mode=_bool(e("SERVICE_DEV_MODE"), True),
            allow_demo_inputs=_bool(e("SERVICE_ALLOW_DEMO"), True),
            signing_key=e("SERVICE_SIGNING_KEY", ""),
            signing_key_file=e("SERVICE_SIGNING_KEY_FILE", ""),
            stripe_secret_key=e("STRIPE_SECRET_KEY", ""),
            stripe_webhook_secret=e("STRIPE_WEBHOOK_SECRET", ""),
            stripe_price_lab=e("STRIPE_PRICE_LAB", ""),
            runner=e("SERVICE_RUNNER", "docker"),
            executor=e("SERVICE_EXECUTOR", "process"),
            pipeline_factory=e("SERVICE_PIPELINE_FACTORY", "service.worker:default_pipeline_factory"),
            poll_seconds=float(e("SERVICE_POLL_SECONDS", "2")),
            heartbeat_stale_s=int(e("SERVICE_HEARTBEAT_STALE_S", "300")),
        )
        if sk:
            s.secret_key, s.secret_key_from_env = sk, True
        return s

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def ensure_data_dir(self) -> Path:
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache(maxsize=1)
def get_settings() -> ServiceSettings:
    return ServiceSettings.from_env()
