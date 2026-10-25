"""Tavily-powered discovery: code repo, errata, prior replication attempts. Plus GitHub repo context."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from . import http
from .arxiv_src import github_links
from .search import Tavily


def discover(search: Tavily, meta: dict, fulltext: str, dataset: str = "") -> dict:
    title = meta["title"]
    queries = {
        "code": (f"{title} official code github", ["github.com"]),
        "erratum": (f"\"{title}\" erratum OR correction OR errata OR retraction", None),
        "replication": (f"\"{title}\" reproducibility replication attempt results differ", None),
        # search for the claim's dataset by name: the paper title only finds other papers
        "data": (f"{dataset} dataset download csv github" if dataset else f"{title} dataset download", None),
    }
    results: dict[str, list[dict]] = {}
    if search.available:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {k: ex.submit(search.search, q, 5, "basic", dom) for k, (q, dom) in queries.items()}
            results = {k: f.result() for k, f in futs.items()}

    candidates = github_links(fulltext) + github_links(meta.get("abstract", ""))
    for r in results.get("code", []):
        candidates += github_links(r.get("url", ""))
    seen, ordered = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)

    evidence = []
    for kind in ("erratum", "replication", "data", "code"):
        for r in results.get(kind, [])[:4]:
            if r.get("error") or not r.get("url"):
                continue
            evidence.append({"kind": kind, "title": r["title"], "url": r["url"], "snippet": r["content"][:300]})
    return {"repo_url": ordered[0] if ordered else None, "candidates": ordered[:5], "evidence": evidence,
            "searched": search.available}


_KW = re.compile(r"errat|correction|corrigendum|retract|replicat|reproduc|could not reproduce|fails? to|discrepan", re.I)

_FILTER_PROMPT = """Paper: {title}

Search results:
{listing}

Return JSON: {{"keep": [{{"i": <index>, "why": "<one short sentence>"}}]}}
Keep ONLY results that specifically report an erratum, correction or retraction of THIS paper, or an attempt
to reproduce/replicate THIS paper's results. Exclude generic explainers, Wikipedia, blog posts about the paper,
tutorials and errata for other papers. Use an empty list if none qualify."""


def readme_notice(readme: str, repo_url: str) -> dict | None:
    """Authors often post their erratum in the repo README (a bug notice, revised results). Surface it as evidence."""
    lines = (readme or "").splitlines()
    for i, line in enumerate(lines):
        if re.search(r"\b(bug|erratum|errata|correction|corrected|revised results?|fixed an? (issue|error))\b", line, re.I):
            # start at the matching line (update lists bury the notice mid-paragraph) and keep a little context
            text = re.sub(r"\s+", " ", re.sub(r"^\s*[-*+]\s*|[#*`>]", "", " ".join(lines[i:i + 3]))).strip()
            if len(text) > 40:
                return {"kind": "erratum", "title": "Authors' notice in the official repository README", "url": repo_url,
                        "snippet": text[:300], "why": "the authors report a bug or correction in their own repository"}
    return None


def _is_paper_itself(url: str, arxiv_id: str | None) -> bool:
    base = re.sub(r"v\d+$", "", arxiv_id or "")
    return bool(base) and base in url and ("arxiv.org" in url or "ar5iv" in url)


def filter_evidence(llm, title: str, evidence: list[dict], arxiv_id: str | None = None) -> list[dict]:
    """Drop search hits that merely mention the paper. Never present a generic page as an erratum."""
    evidence = [e for e in evidence if not _is_paper_itself(e["url"], arxiv_id)]  # the paper is not its own erratum
    cand = [e for e in evidence if e["kind"] in ("erratum", "replication")]
    other = [e for e in evidence if e["kind"] not in ("erratum", "replication")]
    if not cand:
        return other
    kept: list[dict] = []
    done = False
    if getattr(llm, "available", False):
        try:
            listing = "\n".join(f'{i}. [{e["kind"]}] {e["title"]} | {e["url"]} | {e["snippet"][:240]}' for i, e in enumerate(cand))
            out = llm.chat_json("nano", "You are a strict reviewer. Reply with JSON only.",
                                _FILTER_PROMPT.format(title=title, listing=listing), max_tokens=600, temperature=0)
            for k in out.get("keep", []):
                i = int(k["i"])
                if 0 <= i < len(cand):
                    e = dict(cand[i])
                    e["why"] = k.get("why", "")
                    kept.append(e)
            done = True
        except Exception:
            done = False
    if not done:  # offline fallback: must mention errata/replication AND the paper's own title words
        words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", title)][:4]
        for e in cand:
            blob = (e["title"] + " " + e["snippet"]).lower()
            if _KW.search(blob) and sum(w in blob for w in words) >= max(1, len(words) - 1):
                kept.append(e)
    seen, out_list = set(), []
    for e in kept + other:
        if e["url"] not in seen:
            seen.add(e["url"])
            out_list.append(e)
    return out_list


def pick_run_scripts(tree: list[str], dataset: str, limit: int = 3) -> list[str]:
    """Shell/py launch scripts whose path names the claim's dataset (e.g. scripts/FITS/etth1_best.sh).
    They hold the authors' exact command line and hyperparameters, which the README rarely does."""
    tok = re.sub(r"[^a-z0-9]", "", (dataset or "").lower())
    if len(tok) < 3:
        return []
    hits = [p for p in tree if p.lower().endswith((".sh", ".bash", ".yaml", ".yml"))
            and tok in re.sub(r"[^a-z0-9]", "", p.lower())]
    rank = lambda p: (not any(k in p.lower() for k in ("best", "fin", "final", "main")), len(p))
    return sorted(hits, key=rank)[:limit]


def repo_context(repo_url: str, dataset: str = "") -> dict:
    """README, file tree, run scripts for the claim's dataset and pinned HEAD commit (best effort, unauthenticated)."""
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)", repo_url)
    if not m:
        return {}
    owner, repo = m.group(1), m.group(2)
    ctx: dict = {"owner": owner, "repo": repo}
    try:
        info = http.get_json(f"https://api.github.com/repos/{owner}/{repo}", timeout=20)
        branch = info.get("default_branch", "HEAD")
        ctx["default_branch"] = branch
        ctx["description"] = info.get("description")
        ctx["commit"] = http.get_json(f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}", timeout=20).get("sha")
        tree = http.get_json(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1", timeout=30)
        full = [t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"]
        ctx["all_files"] = full
        # big repos are mostly logs/results; show code and configs first so the entry points are visible
        noise = lambda p: bool(re.search(r"(^|/)(logs?|results?|outputs?|checkpoints?|wandb|figures?|imgs?|images?)/", p.lower()))
        code = lambda p: p.lower().endswith((".py", ".sh", ".yaml", ".yml", ".json", ".toml", ".cfg", ".txt", ".md", ".ipynb"))
        ctx["tree"] = sorted(full, key=lambda p: (noise(p), not code(p), p.count("/")))[:200]
    except Exception as e:
        ctx["api_error"] = str(e)[:200]
    for name in ("README.md", "readme.md", "README.rst", "README"):
        try:
            ctx["readme"] = http.request(f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{name}", timeout=20)[:9000]
            break
        except Exception:
            continue
    scripts = {}
    for path in pick_run_scripts(ctx.pop("all_files", None) or ctx.get("tree", []), dataset):
        try:
            scripts[path] = http.request(f"https://raw.githubusercontent.com/{owner}/{repo}/{ctx.get('commit') or 'HEAD'}/{path}",
                                         timeout=20)[:3000]
        except Exception:
            continue
    ctx["run_scripts"] = scripts
    return ctx
