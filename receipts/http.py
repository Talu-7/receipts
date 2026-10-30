"""Tiny stdlib HTTP helpers (honours HTTPS_PROXY / CA env vars automatically)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class HTTPError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


def request(url: str, method: str = "GET", headers: dict | None = None,
            body: dict | None = None, timeout: float = 60) -> str:
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": "receipts/0.1 (+hackathon)", **(headers or {})}
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    # GETs are retried when rate limited (arXiv answers 429 to bursts); POSTs are retried by their callers
    attempts = 4 if method == "GET" else 1
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            err = HTTPError(e.code, e.read().decode("utf-8", "replace"), url)
            if e.code not in (429, 503) or attempt == attempts - 1:
                raise err from None
            try:
                wait = min(30.0, float(e.headers.get("Retry-After") or 0)) or 5.0 * 2 ** attempt
            except ValueError:
                wait = 5.0 * 2 ** attempt
            time.sleep(wait)
    raise RuntimeError("unreachable")


def get_json(url: str, **kw):
    return json.loads(request(url, "GET", **kw))


def post_json(url: str, body: dict, **kw):
    return json.loads(request(url, "POST", body=body, **kw))
