"""Plans, monthly quotas and usage metering."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import Check, UsageRecord, User


@dataclass(frozen=True)
class Plan:
    name: str
    label: str
    checks_per_month: int
    gpu_minutes_per_month: float
    gpu_minutes_per_check: float
    wall_seconds_per_check: int
    max_seeds: int
    price_usd_month: int


PLANS: dict[str, Plan] = {
    "free": Plan("free", "Researcher (free)", checks_per_month=3, gpu_minutes_per_month=60,
                 gpu_minutes_per_check=30, wall_seconds_per_check=45 * 60, max_seeds=5, price_usd_month=0),
    "lab": Plan("lab", "Lab", checks_per_month=100, gpu_minutes_per_month=3000,
                gpu_minutes_per_check=120, wall_seconds_per_check=3 * 3600, max_seeds=20, price_usd_month=499),
}


def period_of(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m")


def plan_for(user: User) -> Plan:
    if user.plan in PLANS and (user.plan == "free" or user.plan_status in ("active", "trialing")):
        return PLANS[user.plan]
    return PLANS["free"]  # a lapsed subscription falls back to the free tier


def usage(s: Session, user: User, period: str | None = None) -> dict:
    period = period or period_of()
    n = s.scalar(select(func.count()).select_from(Check).where(Check.user_id == user.id, Check.period == period)) or 0
    gpu = s.scalar(select(func.coalesce(func.sum(UsageRecord.gpu_minutes), 0.0))
                   .where(UsageRecord.user_id == user.id, UsageRecord.period == period)) or 0.0
    # checks still queued/running are reserved at their cap, so a burst cannot overrun the monthly budget
    reserved = s.scalar(select(func.coalesce(func.sum(Check.gpu_minutes_cap), 0.0)).where(
        Check.user_id == user.id, Check.period == period, Check.status.in_(("queued", "running")))) or 0.0
    p = plan_for(user)
    return {"period": period, "plan": asdict(p), "checks_used": int(n), "checks_left": max(0, p.checks_per_month - int(n)),
            "gpu_minutes_used": round(float(gpu), 3), "gpu_minutes_reserved": round(float(reserved), 3),
            "gpu_minutes_left": round(max(0.0, p.gpu_minutes_per_month - float(gpu) - float(reserved)), 3)}


class QuotaExceeded(Exception):
    pass


def admit(s: Session, user: User, seeds: int | None) -> tuple[int, float, int]:
    """Decide whether a new check is allowed. Returns (seeds, gpu_minutes_cap, wall_seconds_cap)."""
    p = plan_for(user)
    u = usage(s, user)
    if u["checks_left"] <= 0:
        raise QuotaExceeded(f"{p.label} plan: {p.checks_per_month} checks per month used up for {u['period']}")
    if u["gpu_minutes_left"] <= 0.5:
        raise QuotaExceeded(f"{p.label} plan: monthly GPU-minute budget ({p.gpu_minutes_per_month:g}) used up")
    n = max(1, min(int(seeds or min(5, p.max_seeds)), p.max_seeds))
    cap = min(p.gpu_minutes_per_check, u["gpu_minutes_left"])
    return n, round(cap, 3), p.wall_seconds_per_check


def meter(s: Session, check: Check, rec: dict | None, wall_seconds: float) -> UsageRecord:
    """One usage row per check, from the receipt's own infra accounting.

    Sandbox job-seconds are metered against the GPU-minute budget whether or not a GPU was attached:
    simple and conservative. If the worker had to kill a check, wall time stands in for job time.
    """
    infra = (rec or {}).get("infra") or {}
    job_s = float(infra.get("job_seconds") or 0.0)
    if check.status == "failed" and job_s == 0.0:
        job_s = wall_seconds if (check.failure_reason or "").startswith("time cap") else 0.0
    llm = infra.get("llm_usage") or {}
    tokens = 0
    if isinstance(llm, dict):
        for v in llm.values():
            if isinstance(v, dict):
                tokens += int(v.get("total_tokens") or (v.get("prompt_tokens", 0) + v.get("completion_tokens", 0)) or 0)
            elif isinstance(v, (int, float)):
                tokens += int(v)
    existing = s.scalar(select(UsageRecord).where(UsageRecord.check_id == check.id))
    row = existing or UsageRecord(user_id=check.user_id, check_id=check.id, period=check.period)
    row.job_seconds, row.wall_seconds = job_s, float(wall_seconds)
    row.gpu_minutes = round(job_s / 60.0, 4)  # metered as used, even if a run overshot its cap
    row.est_cost_usd = float(infra.get("est_cost_usd") or 0.0)
    row.llm_tokens = tokens
    if not existing:
        s.add(row)
    return row
