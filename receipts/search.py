"""Tavily search: find repos, datasets, errata and prior replication attempts."""
from __future__ import annotations

from . import http
from .config import Settings


class Tavily:
    URL = "https://api.tavily.com/search"

    def __init__(self, settings: Settings):
        self.key = settings.tavily_api_key

    @property
    def available(self) -> bool:
        return bool(self.key)

    def search(self, query: str, max_results: int = 5, depth: str = "basic",
               include_domains: list[str] | None = None) -> list[dict]:
        if not self.available:
            return []
        body = {"query": query, "max_results": max_results, "search_depth": depth}
        if include_domains:
            body["include_domains"] = include_domains
        try:
            data = http.post_json(self.URL, body, headers={"Authorization": f"Bearer {self.key}"}, timeout=40)
        except Exception as e:
            return [{"title": "search failed", "url": "", "content": str(e)[:200], "error": True}]
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "content": (r.get("content") or "")[:600], "score": r.get("score")}
                for r in data.get("results", [])]
