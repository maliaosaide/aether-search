"""URL verification: cross-corroboration + liveness probe.

verify_urls — Tier 0 (corroboration against an evidence set) + Tier 1 probe. Used for
  LLM-generated citations (grok answer, agent answer/digest): these can hallucinate URLs.
probe_urls — Tier 1 only. Used for any URL surfaced to the user (evidence links):
  search engines don't hallucinate URLs, but pages can be dead (404/410) or blocked.
"""

import asyncio
import re
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import settings

_URL_RE = re.compile(r'https?://[^\s<>"\'`，。、；：！？》）】\)\]]+')

ALIVE, DEAD, UNVERIFIED, CORROBORATED = "alive", "dead", "unverified", "corroborated"
_MARK = {ALIVE: "✓", DEAD: "⚠", UNVERIFIED: "?", CORROBORATED: "✓"}

_PROBE_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; aether-search/0.2)"}
_DETAIL = {ALIVE: "alive", DEAD: "HTTP 404/410 — possibly hallucinated", UNVERIFIED: "unreachable/blocked"}


def extract_urls(text: str) -> list[str]:
    seen, urls = set(), []
    for m in _URL_RE.finditer(text):
        url = m.group().rstrip(".,;:!?")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


async def _probe(client: httpx.AsyncClient, url: str) -> str:
    try:
        resp = await client.head(url, follow_redirects=True)
        if resp.status_code in (405, 501):
            resp = await client.get(url, headers={"Range": "bytes=0-0"}, follow_redirects=True)
        if resp.status_code in (404, 410):
            return DEAD
        if resp.status_code < 400:
            return ALIVE
        return UNVERIFIED
    except Exception:
        return UNVERIFIED


async def probe_urls(urls: list[str]) -> dict[str, tuple[str, str]]:
    """Tier 1 only: liveness probe for URLs surfaced to the user."""
    if not urls:
        return {}
    if not settings.verify:
        return {u: (UNVERIFIED, "verification disabled") for u in urls}
    async with httpx.AsyncClient(timeout=settings.verify_timeout, headers=_PROBE_HEADERS) as client:
        statuses = await asyncio.gather(*(_probe(client, u) for u in urls))
    return {u: (s, _DETAIL[s]) for u, s in zip(urls, statuses)}


async def verify_urls(urls: list[str], corroboration: set[str]) -> dict[str, tuple[str, str]]:
    """Tier 0 (corroboration against evidence set) + Tier 1 probe, for LLM-cited URLs."""
    corr_norm = {normalize_url(u) for u in corroboration}
    out: dict[str, tuple[str, str]] = {}
    to_probe = []
    for url in urls:
        if normalize_url(url) in corr_norm:
            out[url] = (CORROBORATED, "corroborated by evidence index")
        else:
            to_probe.append(url)
    out.update(await probe_urls(to_probe))
    return out


def mark(status: str) -> str:
    return _MARK.get(status, "?")
