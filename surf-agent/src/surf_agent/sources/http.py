"""HTTP layer: live fetching (parallel, retries, disk cache) or replay from a saved run."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

RETRY_STATUSES = {429, 500, 502, 503, 504}


class SourceError(RuntimeError):
    pass


class NotCaptured(SourceError):
    """Replay data has no file for this request (e.g. a run saved before the source existed)."""


@dataclass
class Request:
    name: str  # also the raw file stem, e.g. "gridpoint"
    url: str
    kind: str = "json"  # json | text
    headers: dict[str, str] = field(default_factory=dict)
    min_cache_s: int = 0  # cache at least this long even when the server sends no max-age


class Fetcher(Protocol):
    raw: dict[str, str]

    async def fetch(self, req: Request) -> str: ...


def raw_filename(req_name: str, kind: str) -> str:
    return f"{req_name}.{'json' if kind == 'json' else 'txt'}"


def _max_age(headers: httpx.Headers) -> int:
    m = re.search(r"max-age=(\d+)", headers.get("cache-control", ""))
    return int(m.group(1)) if m else 0


class LiveFetcher:
    """Fetches over HTTP. Keeps every response body in `self.raw` so the run can be replayed."""

    def __init__(
        self,
        user_agent: str,
        cache_dir: Path | None = None,
        timeout_s: float = 15,
        retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.raw: dict[str, str] = {}
        self.cache_dir = cache_dir
        self.retries = retries
        self.client = httpx.AsyncClient(
            timeout=timeout_s,
            headers={"User-Agent": user_agent, "Accept": "application/geo+json, application/json, */*"},
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()

    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".json")

    def _cache_get(self, url: str) -> str | None:
        p = self._cache_path(url)
        if not p or not p.exists():
            return None
        entry = json.loads(p.read_text(encoding="utf-8"))
        return entry["body"] if entry["expires"] > time.time() else None

    def _cache_put(self, url: str, body: str, max_age: int) -> None:
        p = self._cache_path(url)
        if not p or max_age <= 0:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"expires": time.time() + max_age, "body": body}), encoding="utf-8")

    async def fetch(self, req: Request) -> str:
        cached = self._cache_get(req.url)
        if cached is not None:
            self.raw[req.name] = cached
            return cached
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = await self.client.get(req.url, headers=req.headers)
                if r.status_code in RETRY_STATUSES and attempt < self.retries:
                    last = SourceError(f"{req.name}: HTTP {r.status_code}")
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    raise SourceError(f"{req.name}: HTTP {r.status_code} from {req.url}")
                body = r.text
                self._cache_put(req.url, body, max(_max_age(r.headers), req.min_cache_s))
                self.raw[req.name] = body
                return body
            except httpx.HTTPError as e:
                last = SourceError(f"{req.name}: {type(e).__name__}: {e}")
                if attempt < self.retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise last or SourceError(f"{req.name}: failed")


class ReplayFetcher:
    """Serves saved raw responses from `<run_dir>/raw/`. Missing files behave like failed sources."""

    def __init__(self, raw_dir: Path):
        self.raw_dir = raw_dir
        self.raw: dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def fetch(self, req: Request) -> str:
        p = self.raw_dir / raw_filename(req.name, req.kind)
        if not p.exists():
            raise NotCaptured(f"{req.name}: not in replay data ({p.name})")
        body = p.read_text(encoding="utf-8")
        self.raw[req.name] = body
        return body


async def fetch_all(fetcher: Fetcher, reqs: list[Request]) -> dict[str, str | Exception]:
    """Fetch in parallel; a failure is returned as the exception, never raised."""
    results = await asyncio.gather(*(fetcher.fetch(r) for r in reqs), return_exceptions=True)
    return {r.name: res for r, res in zip(reqs, results)}
