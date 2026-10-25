"""Job runners. Same payload, two backends:

  NebiusRunner - one Nebius Serverless Job per seed (the real thing, run in parallel)
  LocalRunner  - same entrypoint in a local subprocess (dev, offline demo, `verify`)
"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

ENTRY = str(Path(__file__).with_name("sandbox_entry.py"))


@dataclass
class JobSpec:
    script: str
    seed: int
    run_id: str = "run"
    repo_url: str | None = None
    repo_commit: str | None = None
    requirements: list[str] = field(default_factory=list)
    install_repo_requirements: bool = False
    install: bool = True
    timeout_s: int = 1500
    env: dict = field(default_factory=dict)
    no_network: bool = False

    def payload_b64(self) -> str:
        d = {
            "script": self.script, "seed": self.seed, "repo_url": self.repo_url,
            "repo_commit": self.repo_commit, "requirements": self.requirements,
            "install_repo_requirements": self.install_repo_requirements, "install": self.install,
            "timeout_s": self.timeout_s, "env": self.env, "no_network": self.no_network,
        }
        return base64.b64encode(json.dumps(d).encode()).decode()


@dataclass
class JobResult:
    seed: int
    ok: bool
    value: float | None = None
    metric: str | None = None
    extras: dict = field(default_factory=dict)
    logs: str = ""
    seconds: float = 0.0
    job_id: str = ""
    backend: str = ""
    meta: dict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("seed", "ok", "value", "metric", "extras", "seconds", "job_id", "backend", "meta", "error")}


def _parse_result(payload: str) -> dict | None:
    """JSON, or failing that a Python dict literal: generated scripts often f-string a dict repr into the line."""
    try:
        d = json.loads(payload)
    except ValueError:
        import ast
        py = re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False", re.sub(r"\bnull\b", "None", payload)))
        try:
            d = ast.literal_eval(py)
        except (ValueError, SyntaxError):
            return None
    return d if isinstance(d, dict) else None


def parse_logs(seed: int, logs: str, seconds: float, job_id: str, backend: str, returncode_ok: bool) -> JobResult:
    result = None
    meta: dict = {}
    err = ""
    for line in logs.splitlines():
        if line.startswith("RECEIPTS_RESULT "):
            result = _parse_result(line[len("RECEIPTS_RESULT "):])
            if result is None:
                err = "unparseable RECEIPTS_RESULT line"
        elif line.startswith("RECEIPTS_META "):
            try:
                meta.update(json.loads(line[len("RECEIPTS_META "):]))
            except ValueError:
                pass
        elif line.startswith("RECEIPTS_ERROR "):
            err = line[len("RECEIPTS_ERROR "):]
    if result is not None and isinstance(result.get("value"), (int, float)):
        return JobResult(seed, True, float(result["value"]), result.get("metric"), result.get("extras") or {},
                         logs, seconds, job_id, backend, meta, "")
    if not err:
        err = "script exited without a RECEIPTS_RESULT line" if returncode_ok else "script failed"
    return JobResult(seed, False, None, None, {}, logs, seconds, job_id, backend, meta, err)


class LocalRunner:
    name = "local"

    def run(self, spec: JobSpec) -> JobResult:
        t0 = time.time()
        try:
            p = subprocess.run([sys.executable, ENTRY, spec.payload_b64()], capture_output=True, text=True,
                               timeout=spec.timeout_s + 1800)
            logs = (p.stdout or "") + (p.stderr or "")
            return parse_logs(spec.seed, logs, time.time() - t0, f"local-{os.getpid()}-{spec.seed}", self.name, p.returncode == 0)
        except subprocess.TimeoutExpired as e:
            logs = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return JobResult(spec.seed, False, logs=logs, seconds=time.time() - t0, backend=self.name, error="timeout")


def clamp_job_timeout(value: str, minimum_s: int = 3600) -> str:
    """Nebius jobs accept a timeout of 1h to 168h; anything shorter is rejected, so raise it to 1h.

    The reproduction script has its own, shorter budget (JobSpec.timeout_s); this is only the job's outer limit.
    """
    m = re.fullmatch(r"\s*(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?\s*", value or "")
    if not m or not any(m.groups()):
        return "1h"
    secs = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)
    return value.strip() if secs >= minimum_s else "1h"


_TERMINAL_OK = {"COMPLETED", "SUCCEEDED", "SUCCESS", "FINISHED"}
_TERMINAL_BAD = {"FAILED", "ERROR", "CANCELLED", "CANCELED", "TIMEOUT", "TIMED_OUT"}


def _find_state(obj) -> str | None:
    """Locate a job state string anywhere in a `nebius ... get --format json` document."""
    if isinstance(obj, dict):
        for k in ("state", "status"):
            v = obj.get(k)
            if isinstance(v, str):
                return v.upper()
        for v in obj.values():
            s = _find_state(v)
            if s:
                return s
    elif isinstance(obj, list):
        for v in obj:
            s = _find_state(v)
            if s:
                return s
    return None


class NebiusRunner:
    """Runs each reproduction as a Nebius Serverless Job via the `nebius` CLI.

    Assumes `nebius` is installed and configured (project ID in ~/.nebius/config.yaml)
    and that `image` is the receipts sandbox image (see sandbox/Dockerfile).
    """
    name = "nebius"

    def __init__(self, image: str, platform: str, preset: str = "", extra_args: str = "",
                 timeout: str = "30m", cost_per_hour: float = 0.0, poll_s: float = 6.0,
                 profile: str = "", parent_id: str = ""):
        self.image, self.platform, self.preset = image, platform, preset
        self.extra_args, self.timeout, self.cost_per_hour, self.poll_s = extra_args, clamp_job_timeout(timeout), cost_per_hour, poll_s
        self.profile, self.parent_id = profile, parent_id  # optional: `nebius --profile X`, `--parent-id <project>`

    @staticmethod
    def cli_available() -> bool:
        from shutil import which
        return which("nebius") is not None

    def _cli(self, *args: str, timeout: int = 180):
        base = ["nebius"] + (["--profile", self.profile] if self.profile else [])
        try:
            p = subprocess.run([*base, *args], capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return 127, "", "nebius CLI not found on PATH (it installs to ~/.nebius/bin; run from a shell that has it)"
        except subprocess.TimeoutExpired:
            return 124, "", f"nebius {' '.join(args[:3])} timed out after {timeout}s"
        return p.returncode, (p.stdout or ""), (p.stderr or "")

    def run(self, spec: JobSpec) -> JobResult:
        t0 = time.time()
        name = re.sub(r"[^a-z0-9-]", "-", f"receipts-{spec.run_id}-s{spec.seed}".lower())[:60].strip("-")
        cmd = ["ai", "job", "create", "--name", name, "--image", self.image, "--platform", self.platform,
               "--timeout", self.timeout, "--args", spec.payload_b64(),
               # without --async the CLI blocks on the job; we poll ourselves below
               "--async", "--format", "jsonpath={.resource_id}"]
        if self.preset:
            cmd += ["--preset", self.preset]
        if self.parent_id:
            cmd += ["--parent-id", self.parent_id]
        if self.extra_args:
            cmd += shlex.split(self.extra_args)
        rc, out, err = self._cli(*cmd)
        if rc != 0:
            return JobResult(spec.seed, False, logs=out + err, seconds=time.time() - t0, backend=self.name,
                             error="job create failed: " + (err or out)[:300])
        job_id = out.strip() if re.fullmatch(r"[A-Za-z0-9-]+", out.strip()) else ""
        if not job_id:
            m = re.search(r"\b(aijob-[a-z0-9]+)\b", out + err)
            job_id = m.group(1) if m else ""
        if not job_id:
            rc, out2, err2 = self._cli("ai", "job", "get-by-name", "--name", name, "--format", "json")
            try:
                d = json.loads(out2)
                job_id = (d.get("metadata") or {}).get("id") or d.get("id") or ""
            except ValueError:
                pass
        if not job_id:
            return JobResult(spec.seed, False, logs=out + err, seconds=time.time() - t0, backend=self.name,
                             error="could not determine job id")

        deadline = time.time() + 3600
        state = ""
        while time.time() < deadline:
            rc, o, e = self._cli("ai", "job", "get", job_id, "--format", "json")
            try:
                state = _find_state(json.loads(o)) or ""
            except ValueError:
                mm = re.search(r"state:\s*([A-Za-z_]+)", o)
                state = mm.group(1).upper() if mm else ""
            if state in _TERMINAL_OK or state in _TERMINAL_BAD:
                break
            time.sleep(self.poll_s)

        rc, logs, lerr = self._cli("ai", "job", "logs", job_id, timeout=300)
        logs = logs + lerr
        self._cli("ai", "job", "delete", job_id)  # best effort cleanup
        secs = time.time() - t0
        res = parse_logs(spec.seed, logs, secs, job_id, self.name, state in _TERMINAL_OK)
        res.meta["final_state"] = state
        res.meta["image"] = self.image
        res.meta["platform"] = self.platform
        res.meta["est_cost_usd"] = round(secs / 3600 * self.cost_per_hour, 4)
        return res


class DockerRunner:
    """Runs the sandbox image locally with `docker run`: the same image and entrypoint a Nebius job uses.

    Hardening: every Linux capability dropped, no-new-privileges, and pid/memory/cpu limits.
    This is NOT a perfect security boundary (the container shares the host kernel).
    Network: disabled when nothing has to be downloaded (no repo clone, no pip install); otherwise the
    container keeps normal network access, because cloning and installing need it. The entrypoint's
    own `no_network` switch (`unshare -rn`) needs a capability we drop, so it is not used here.
    """
    name = "docker"

    def __init__(self, image: str, memory: str = "4g", cpus: str = "2", pids: int = 512, gpus: bool = False,
                 shm: str = "1g"):
        self.image, self.memory, self.cpus, self.pids, self.gpus, self.shm = image, memory, cpus, pids, gpus, shm
        self._image_id: str | None = None

    @staticmethod
    def available() -> bool:
        from shutil import which
        return which("docker") is not None

    def _inspect_image(self) -> str:
        if self._image_id is None:
            try:
                p = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", self.image],
                                   capture_output=True, text=True, timeout=30)
                self._image_id = p.stdout.strip() if p.returncode == 0 else ""
            except Exception:
                self._image_id = ""
        return self._image_id

    def command(self, spec: JobSpec, container_name: str) -> list[str]:
        spec = replace(spec, no_network=False)
        needs_net = bool(spec.repo_url) or spec.install
        cmd = ["docker", "run", "--rm", "--name", container_name, "--cap-drop=ALL", "--security-opt=no-new-privileges",
               "--pids-limit", str(self.pids), "--memory", self.memory, "--cpus", self.cpus,
               # Docker's default 64 MB /dev/shm kills PyTorch DataLoader workers with a bus error
               "--shm-size", self.shm]
        if not needs_net:
            cmd += ["--network", "none"]
        if self.gpus:
            cmd += ["--gpus", "all"]
        return cmd + [self.image, spec.payload_b64()]

    def run(self, spec: JobSpec) -> JobResult:
        t0 = time.time()
        if not self.available():
            return JobResult(spec.seed, False, backend=self.name,
                             error="docker not found: install Docker Desktop (and enable WSL integration if you use WSL)")
        cname = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"receipts-{spec.run_id}-s{spec.seed}-{int(t0 * 1000) % 10**8}")
        try:
            p = subprocess.run(self.command(spec, cname), capture_output=True, text=True, timeout=spec.timeout_s + 1800)
        except subprocess.TimeoutExpired as e:
            subprocess.run(["docker", "rm", "-f", cname], capture_output=True)  # the client was killed, not the container
            out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return JobResult(spec.seed, False, logs=out, seconds=time.time() - t0, job_id=cname, backend=self.name, error="timeout")
        logs = (p.stdout or "") + (p.stderr or "")
        res = parse_logs(spec.seed, logs, time.time() - t0, cname, self.name, p.returncode == 0)
        if p.returncode == 125:  # docker itself failed to start the container
            res.ok, res.value = False, None
            res.error = "docker could not start the container: " + logs.strip()[-300:]
        res.meta.update(image=self.image, image_id=self._inspect_image(), platform="docker-local")
        return res
