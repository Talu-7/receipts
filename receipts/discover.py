"""Tavily-powered discovery: code repo, errata, prior replication attempts. Plus GitHub repo context."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from . import http
from .arxiv_src import github_links
from .search import Tavily


def discover(search: Tavily, meta: dict, fulltext: str) -> dict:
    title = meta["title"]
    queries = {
        "code": (f"{title} official code github", ["github.com"]),
        "erratum": (f"\"{title}\" erratum OR correction OR errata OR retraction", None),
        "replication": (f"\"{title}\" reproducibility replication attempt results differ", None),
        "data": (f"{title} dataset download", None),
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


def filter_evidence(llm, title: str, evidence: list[dict]) -> list[dict]:
    """Drop search hits that merely mention the paper. Never present a generic page as an erratum."""
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


def repo_context(repo_url: str) -> dict:
    """README, file tree and pinned HEAD commit for a GitHub repo (best effort, unauthenticated)."""
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
        ctx["tree"] = [t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"][:200]
    except Exception as e:
        ctx["api_error"] = str(e)[:200]
    for name in ("README.md", "readme.md", "README.rst", "README"):
        try:
            ctx["readme"] = http.request(f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{name}", timeout=20)[:9000]
            break
        except Exception:
            continue
    return ctx
