"""Nemotron Super writes the reproduction script (and repairs it when the probe run fails)."""
from __future__ import annotations

import re

from .llm import LLM

SYSTEM = """You write reproduction scripts for ML papers that run inside an isolated sandbox.
Rules:
- The sandbox has already cloned the repo at a pinned commit and set the working directory to the repo root.
- The script reads the integer seed from the SEED environment variable and must be deterministic given it.
- Run the repo's OWN evaluation code path for the headline claim. Never hard-code, copy or tune towards
  the claimed number, and never fabricate results.
- Budget: <= 20 minutes on one GPU. If you must shrink anything (subset, fewer epochs, pretrained
  checkpoint instead of training), say so in extras["deviations"].
- If the paper/code leave a protocol choice ambiguous (e.g. preprocessing, split, metric variant), compute
  the main number by the protocol the paper DESCRIBES and put alternatives in extras as numeric
  entries named variant_<what> so a mismatch can be diagnosed.
- Print exactly one final line:  RECEIPTS_RESULT {"metric": "...", "value": <float in the claim's units>, "extras": {...}}
  Build it with json.dumps, e.g. print("RECEIPTS_RESULT " + json.dumps({...})), never a dict repr.
- Print progress to stdout so logs are useful.
- If you cannot obtain the real metric, exit with a non-zero status and print NO RECEIPTS_RESULT line.
  Never print a placeholder value (-1, 0, nan) in its place.
Output format, nothing else:
```requirements
<one pip requirement per line, may be empty>
```
```python
<the script>
```"""

PROMPT = """Claim to reproduce: {statement}
Metric: {metric} | claimed value: {value}{unit} | dataset: {dataset} | location: {where}

Paper: {title}
Repo: {repo_url}  (commit {commit})
Repo description: {description}

File tree (truncated):
{tree}

README (truncated):
{readme}
{scripts}{repair}"""


def _blocks(text: str) -> tuple[str, list[str]]:
    py = re.search(r"```(?:python3?|py)\s*\n(.*?)```", text, re.S)
    rq = re.search(r"```requirements\s*\n(.*?)```", text, re.S)
    if not py:
        raise ValueError("model did not return a python block")
    reqs = [l.strip() for l in (rq.group(1) if rq else "").splitlines() if l.strip() and not l.startswith("#")]
    return py.group(1).strip() + "\n", reqs


def lint_script(script: str, claimed: float) -> str | None:
    """Cheap integrity guard: the script must compute its number, not state the claimed one."""
    if "RECEIPTS_RESULT" not in script:
        return "script never prints RECEIPTS_RESULT"
    try:  # a syntax error should not cost a sandbox run
        compile(script, "repro.py", "exec")
    except SyntaxError as e:
        return f"script has a SyntaxError at line {e.lineno}: {e.msg}"
    for s in {f"{claimed}", f"{claimed:.1f}", f"{claimed:.2f}"}:
        if re.search(rf"(?<![\d.]){re.escape(s)}(?![\d])", script):
            return f"script contains the claimed value {s} as a literal"
    return None


def generate_script(llm: LLM, meta: dict, claim: dict, repo_url: str, ctx: dict,
                    previous: str | None = None, failure: str | None = None) -> dict:
    repair = ""
    if previous and failure:
        repair = ("\n\nYour previous script failed in the sandbox.\nPrevious script:\n```python\n"
                  f"{previous[:6000]}\n```\nFailure summary:\n{failure[:2500]}\nFix the script (and requirements).")
    prompt = PROMPT.format(
        statement=claim["statement"], metric=claim.get("metric"), value=claim["value"], unit=claim.get("unit", ""),
        dataset=claim.get("dataset"), where=claim.get("where"), title=meta["title"], repo_url=repo_url,
        commit=ctx.get("commit", "HEAD"), description=ctx.get("description"),
        tree="\n".join(ctx.get("tree", [])[:150]) or "(unavailable)", readme=(ctx.get("readme") or "(unavailable)")[:7000],
        scripts="".join(f"\nAuthors' run script {p} (use its entry point and hyperparameters):\n{body}\n"
                        for p, body in (ctx.get("run_scripts") or {}).items())
                + ("\nDataset sources found by web search (check them; prefer raw file URLs):\n" + "\n".join(ctx["data_links"]) + "\n"
                   if ctx.get("data_links") else ""),
        repair=repair)
    last_problem = None
    for _ in range(3):
        fix = (f"\n\nFix this problem: {last_problem}. The claimed number must not appear anywhere in the script, "
               "not even in comments, strings or prints; the comparison with the claim happens outside the sandbox."
               if last_problem else "")
        text = llm.chat("super", SYSTEM, prompt + fix,
                        max_tokens=6000, temperature=0.2)
        try:
            script, reqs = _blocks(text)
        except ValueError as e:  # formatting slip: ask again
            last_problem = f"{e}; reply with a ```requirements block and a ```python block"
            continue
        last_problem = lint_script(script, claim["value"])
        if not last_problem:
            return {"script": script, "requirements": reqs}
    raise RuntimeError(f"could not generate an acceptable script: {last_problem}")
