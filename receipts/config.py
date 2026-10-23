"""Settings, loaded from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Settings:
    # Nebius Token Factory (OpenAI-compatible) -> Nemotron Nano / Super / Ultra
    nebius_api_key: str
    llm_base_url: str
    nano_model: str
    super_model: str
    ultra_model: str
    # Tavily
    tavily_api_key: str
    # Nebius Serverless Jobs (via the `nebius` CLI)
    job_image: str
    job_platform: str
    job_preset: str
    job_extra_args: str
    job_timeout: str
    job_cost_per_hour: float
    # Behaviour
    default_runner: str
    max_parallel: int
    out_dir: str

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        e = os.environ.get
        return cls(
            nebius_api_key=e("NEBIUS_API_KEY", ""),
            llm_base_url=e("NEBIUS_BASE_URL", "https://api.tokenfactory.us-central1.nebius.com/v1/"),
            # Exact IDs are auto-resolved from /v1/models at runtime; these are fallbacks.
            nano_model=e("NEMOTRON_NANO_MODEL", "nvidia/nemotron-3-nano-30b-a3b"),
            super_model=e("NEMOTRON_SUPER_MODEL", "nvidia/nemotron-3-super-120b-a12b"),
            ultra_model=e("NEMOTRON_ULTRA_MODEL", "nvidia/nemotron-3-ultra-550b-a55b"),
            tavily_api_key=e("TAVILY_API_KEY", ""),
            job_image=e("RECEIPTS_JOB_IMAGE", "ghcr.io/YOUR_ORG/receipts-sandbox:latest"),
            job_platform=e("RECEIPTS_JOB_PLATFORM", "gpu-l40s-a"),
            job_preset=e("RECEIPTS_JOB_PRESET", ""),
            job_extra_args=e("RECEIPTS_JOB_EXTRA_ARGS", ""),
            job_timeout=e("RECEIPTS_JOB_TIMEOUT", "1h"),
            job_cost_per_hour=float(e("RECEIPTS_JOB_COST_PER_HOUR", "0") or 0),
            default_runner=e("RECEIPTS_RUNNER", "local"),
            max_parallel=int(e("RECEIPTS_MAX_PARALLEL", "8")),
            out_dir=e("RECEIPTS_OUT_DIR", "out"),
        )
