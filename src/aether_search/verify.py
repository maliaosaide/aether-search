"""Verification of grok-cited URLs: cross-corroboration + liveness probe.

Tier 0: URL also present in aux index results -> corroborated (free).
Tier 1: HEAD probe (Range-GET fallback on 405/501), status only, no body.
404/410 -> dead; 2xx/3xx -> alive; anything else -> unverified (anti-bot != nonexistent).
"""

import asyncio
import re
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import settings

_URL_RE = re.compile(r'https?://[^\s<>"\'`，。、；：！？》）】\)\]]+')

ALIVE, DEAD, UNVERIFIED, CORROBORATED = "alive", "dead", "unverified", "corroborated"
_MARK = {ALIVE: "✓", DEAD: "⚠", UNVERIFIED: "?", CORROBORATED: "✓"}


def extract_urls(text: str) -> list[str]:
    seen, urls = set(), []
    for m in _URL_RE.finditer(text):
        url = m.group().rstrip(".,;:!?")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _norm(url: str) -> str:
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


async def verify_urls(grok_urls: list[str], aux_urls: list[str]) -> dict[str, tuple[str, str]]:
    """url -> (status, detail). Aux-corroborated URLs skip probing."""
    aux_norm = {_norm(u) for u in aux_urls}
    out: dict[str, tuple[str, str]] = {}
    to_probe = []
    for url in grok_urls:
        if _norm(url) in aux_norm:
            out[url] = (CORROBORATED, "corroborated by aux index")
        else:
            to_probe.append(url)

    if to_probe and settings.verify:
        async with httpx.AsyncClient(
            timeout=settings.verify_timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; aether-search/0.1)"},
        ) as client:
            statuses = await asyncio.gather(*(_probe(client, u) for u in to_probe))
        detail = {ALIVE: "alive", DEAD: "HTTP 404/410 — possibly hallucinated", UNVERIFIED: "unreachable/blocked"}
        for url, status in zip(to_probe, statuses):
            out[url] = (status, detail[status])
    else:
        for url in to_probe:
            out[url] = (UNVERIFIED, "verification disabled")
    return out


def mark(status: str) -> str:
    return _MARK.get(status, "?")
