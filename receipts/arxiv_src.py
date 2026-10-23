"""arXiv ingestion: metadata via the arXiv API, full text via the HTML render."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html import unescape
from html.parser import HTMLParser

from . import http

_ID_RE = re.compile(r"(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?", re.I)


def parse_arxiv_id(s: str) -> str | None:
    s = s.strip()
    m = _ID_RE.search(s)
    return m.group(1) if m else None


def fetch_metadata(arxiv_id: str) -> dict:
    xml = http.request(f"https://export.arxiv.org/api/query?id_list={arxiv_id}", timeout=30)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = ET.fromstring(xml).find("a:entry", ns)
    if entry is None:
        raise ValueError(f"arXiv id {arxiv_id} not found")
    g = lambda tag: re.sub(r"\s+", " ", (entry.findtext(f"a:{tag}", default="", namespaces=ns) or "")).strip()
    return {
        "arxiv_id": arxiv_id,
        "title": g("title"),
        "abstract": g("summary"),
        "published": g("published"),
        "authors": [re.sub(r"\s+", " ", a.findtext("a:name", default="", namespaces=ns)) for a in entry.findall("a:author", ns)],
        "url": f"https://arxiv.org/abs/{arxiv_id}",
    }


class _Text(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "svg", "math"}

    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag in {"p", "tr", "br", "h1", "h2", "h3", "h4", "li", "table", "figcaption"}:
            self.out.append("\n")
        if tag in {"td", "th"}:
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, d):
        if not self._skip:
            self.out.append(d)


def fetch_fulltext(arxiv_id: str, limit: int = 70_000) -> str:
    """Best-effort full text (HTML render keeps tables readable). Returns '' on failure."""
    for url in (f"https://arxiv.org/html/{arxiv_id}", f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}"):
        try:
            html = http.request(url, timeout=45)
        except Exception:
            continue
        p = _Text()
        p.feed(html)
        text = unescape("".join(p.out))
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        if len(text) > 2000:
            return text[:limit]
    return ""


GITHUB_RE = re.compile(r"https?://(?:www\.)?github\.com/([\w.\-]+)/([\w.\-]+)", re.I)


def github_links(text: str) -> list[str]:
    """GitHub repos mentioned in the text, most frequent first."""
    counts: dict[str, int] = {}
    for m in GITHUB_RE.finditer(text or ""):
        owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2)).rstrip(".,);]")
        if owner.lower() in {"features", "topics", "orgs", "sponsors"}:
            continue
        url = f"https://github.com/{owner}/{repo}"
        counts[url] = counts.get(url, 0) + 1
    return [u for u, _ in sorted(counts.items(), key=lambda kv: -kv[1])]
