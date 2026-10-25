#!/usr/bin/env python3
"""Sandbox entrypoint. Runs inside a Nebius Serverless Job container (or locally).

usage: sandbox_entry.py <base64-encoded JSON payload>

payload keys:
  script        python source of the reproduction (reads SEED from env, prints one
                `RECEIPTS_RESULT {json}` line)
  seed          int
  repo_url      optional git repo to clone (script runs with cwd = repo root)
  repo_commit   optional commit/tag to check out (default: default branch HEAD)
  requirements  optional list of pip requirement strings
  install_repo_requirements  bool, pip install -r requirements.txt from the repo
  install       bool (default true); false skips every pip step (pre-baked images / local demos)
  timeout_s     wall-clock budget for the script itself
  env           extra environment variables
  no_network    bool; run the script in a fresh network namespace (after install) when the
                kernel allows it via `unshare -rn`

Only stdlib is used so the entrypoint works in any python image.
"""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time


def meta(**kw):
    print("RECEIPTS_META " + json.dumps(kw, sort_keys=True), flush=True)


def sh(cmd, cwd=None, timeout=900, env=None):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out


def _installed(dist):
    try:
        from importlib.metadata import version
        version(dist)
        return True
    except Exception:
        return False


def main():
    payload = json.loads(base64.b64decode(sys.argv[1]).decode())
    work = tempfile.mkdtemp(prefix="receipts-")
    cwd = work
    t0 = time.time()
    script = payload["script"]
    meta(seed=payload.get("seed"), script_sha256=hashlib.sha256(script.encode()).hexdigest(),
         python=sys.version.split()[0])

    repo_url = payload.get("repo_url")
    if repo_url:
        repo_dir = os.path.join(work, "repo")
        rc, out = sh(["git", "clone", "--quiet", repo_url, repo_dir], timeout=600)
        if rc != 0:
            print(out)
            print("RECEIPTS_ERROR clone_failed")
            sys.exit(2)
        if payload.get("repo_commit"):
            rc, out = sh(["git", "checkout", "--quiet", payload["repo_commit"]], cwd=repo_dir)
            if rc != 0:
                print(out)
                print("RECEIPTS_ERROR checkout_failed")
                sys.exit(2)
        rc, head = sh(["git", "rev-parse", "HEAD"], cwd=repo_dir)
        meta(repo_url=repo_url, repo_commit=head.strip())
        cwd = repo_dir

    if payload.get("install", True):
        reqs = list(payload.get("requirements") or [])
        if reqs:
            rc, out = sh([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", *reqs], timeout=1500)
            if rc != 0:
                print(out[-4000:])
                print("RECEIPTS_ERROR pip_install_failed")
                sys.exit(3)
        rq = os.path.join(cwd, "requirements.txt")
        if payload.get("install_repo_requirements") and os.path.exists(rq):
            rc, out = sh([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", rq], timeout=1500)
            if rc != 0:
                # Old repos pin versions that no longer install. Fall back to best effort: one package at a time,
                # exact pins relaxed, and never replace a torch that is already installed (the image's CUDA build).
                errs = [l for l in out.splitlines() if l.startswith("ERROR")]
                print("exact repo requirements failed (" + "; ".join(e[:160] for e in errs[-2:]) + "); retrying with relaxed pins",
                      flush=True)
                skipped, failed, relaxed = [], [], []
                for line in open(rq, encoding="utf-8", errors="replace").read().splitlines():
                    line = line.split("#", 1)[0].strip()
                    if not line or line.startswith("-"):
                        continue
                    name = re.split(r"[=<>!~\[; ]", line, 1)[0].strip()
                    if name.lower() in ("torch", "torchvision", "torchaudio") and _installed(name):
                        skipped.append(line)
                        continue
                    # keep the authors' exact pin when it still installs; relax only the ones that do not
                    rc1, _ = sh([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", line], timeout=900)
                    if rc1 != 0:
                        rc1, _ = sh([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", name], timeout=900)
                        (relaxed if rc1 == 0 else failed).append(line if rc1 == 0 else name)
                meta(repo_requirements="partly_relaxed", relaxed_pins=relaxed, kept_preinstalled=skipped, uninstallable=failed)

    path = os.path.join(work, "repro.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(script)

    env = dict(os.environ)
    env.update({k: str(v) for k, v in (payload.get("env") or {}).items()})
    env["SEED"] = str(payload.get("seed", 0))
    env["PYTHONHASHSEED"] = env["SEED"]
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [sys.executable, path]
    isolated = False
    if payload.get("no_network") and shutil.which("unshare"):
        probe = subprocess.run(["unshare", "-rn", "true"], capture_output=True)
        if probe.returncode == 0:
            cmd = ["unshare", "-rn", *cmd]
            isolated = True
    meta(network_isolated=isolated)
    if payload.get("no_network") and not isolated:
        # fail closed: never run untrusted code with network access when isolation was requested
        print("RECEIPTS_ERROR network_isolation_unavailable (refusing to run with network access)")
        sys.exit(5)

    timeout_s = int(payload.get("timeout_s", 1500))
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout_s)
        meta(exit_code=p.returncode, run_seconds=round(time.time() - t0, 2))
        sys.exit(0 if p.returncode == 0 else 1)
    except subprocess.TimeoutExpired:
        print(f"RECEIPTS_ERROR timeout after {timeout_s}s")
        sys.exit(4)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
