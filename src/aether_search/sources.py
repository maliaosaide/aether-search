"""Source parsing + session cache.

Reference: grok-search sources.py (MIT, Copyright (c) 2025 GuDaStudio), rewritten aether-style.
URL extraction reuses verify.extract_urls; normalization reuses verify.normalize_url — no
grok-search utils dependency (different regex char classes, must not mix).
"""

import ast
import json
import re
import uuid
from collections import OrderedDict
from typing import Any

import asyncio

from .verify import extract_urls, normalize_url

_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_HEADING = re.compile(
    r"(?im)^"
    r"(?:#{1,6}\s*)?"
    r"(?:\*\*|__)?\s*"
    r"(sources?|references?|citations?|信源|参考资料|参考|引用|来源列表|来源)"
    r"\s*(?:\*\*|__)?"
    r"(?:\s*[（(][^)\n]*[)）])?"
    r"\s*[:：]?\s*$"
)
_FUNC = re.compile(
    r"(?im)(^|\n)\s*(sources|source|citations|citation|references|reference|citation_card|source_cards|source_card)\s*\("
)


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


class SourcesCache:
    def __init__(self, max_size: int = 256):
        self._max_size = max_size
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[str, list[dict]] = OrderedDict()

    async def set(self, session_id: str, sources: list[dict]) -> None:
        async with self._lock:
            self._cache[session_id] = sources
            self._cache.move_to_end(session_id)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    async def get(self, session_id: str) -> list[dict] | None:
        async with self._lock:
            sources = self._cache.get(session_id)
            if sources is None:
                return None
            self._cache.move_to_end(session_id)
            return sources


def merge_sources(*source_lists: list[dict]) -> list[dict]:
    seen: set[str] = set()
    merged: list[dict] = []
    for sources in source_lists:
        for item in sources or []:
            url = (item or {}).get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            key = normalize_url(url.strip())
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def source_to_dict(s) -> dict:
    return {"url": s.url, "title": s.title or "", "content": s.content or "", "connector": s.connector or ""}


def split_answer_and_sources(text: str) -> tuple[str, list[dict]]:
    raw = (text or "").strip()
    if not raw:
        return "", []
    for fn in (_split_function_call, _split_heading, _split_details, _split_tail):
        out = fn(raw)
        if out:
            return out
    return raw, []


def _split_function_call(text: str) -> tuple[str, list[dict]] | None:
    matches = list(_FUNC.finditer(text))
    if not matches:
        return None
    for m in reversed(matches):
        extracted = _extract_balanced(text, m.end() - 1)
        if not extracted:
            continue
        _, args = extracted
        sources = _parse_payload(args)
        if sources:
            return text[: m.start()].rstrip(), sources
    return None


def _extract_balanced(text: str, open_idx: int) -> tuple[int, str] | None:
    if open_idx < 0 or open_idx >= len(text) or text[open_idx] != "(":
        return None
    depth = 1
    in_str: str | None = None
    esc = False
    for i in range(open_idx + 1, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == in_str:
                in_str = None
            continue
        if ch in ("'", '"'):
            in_str = ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                if text[i + 1:].strip():
                    return None
                return i, text[open_idx + 1: i]
    return None


def _split_heading(text: str) -> tuple[str, list[dict]] | None:
    matches = list(_HEADING.finditer(text))
    if not matches:
        return None
    for m in reversed(matches):
        sources = _extract_links(text[m.start():])
        if sources:
            return text[: m.start()].rstrip(), sources
    return None


def _split_details(text: str) -> tuple[str, list[dict]] | None:
    low = text.lower()
    close = low.rfind("</details>")
    if close == -1 or text[close + len("</details>"):].strip():
        return None
    open_ = low.rfind("<details", 0, close)
    if open_ == -1:
        return None
    sources = _extract_links(text[open_: close + len("</details>")])
    if len(sources) < 2:
        return None
    return text[:open_].rstrip(), sources


def _split_tail(text: str) -> tuple[str, list[dict]] | None:
    lines = text.splitlines()
    idx = len(lines) - 1
    while idx >= 0 and not lines[idx].strip():
        idx -= 1
    if idx < 0:
        return None
    tail_end = idx
    count = 0
    while idx >= 0:
        line = lines[idx].strip()
        if not line:
            idx -= 1
            continue
        if not _is_link_line(line):
            break
        count += 1
        idx -= 1
    if count < 2:
        return None
    sources = _extract_links("\n".join(lines[idx + 1: tail_end + 1]))
    if not sources:
        return None
    return "\n".join(lines[: idx + 1]).rstrip(), sources


def _is_link_line(line: str) -> bool:
    stripped = re.sub(r"^\s*(?:[-*]|\d+\.)\s*", "", line).strip()
    if not stripped:
        return False
    return stripped.startswith(("http://", "https://")) or bool(_MD_LINK.search(stripped))


def _parse_payload(payload: str) -> list[dict]:
    payload = (payload or "").strip().rstrip(";")
    if not payload:
        return []
    data: Any = None
    try:
        data = json.loads(payload)
    except Exception:
        try:
            data = ast.literal_eval(payload)
        except Exception:
            data = None
    if data is None:
        return _extract_links(payload)
    if isinstance(data, dict):
        for key in ("sources", "citations", "references", "urls"):
            if key in data:
                return _normalize(data[key])
        return _normalize(data)
    return _normalize(data)


def _normalize(data: Any) -> list[dict]:
    items = data if isinstance(data, (list, tuple)) else [data]
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            for url in extract_urls(item):
                k = normalize_url(url)
                if k not in seen:
                    seen.add(k)
                    out.append({"url": url})
            continue
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            title, url = item[0], item[1]
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                k = normalize_url(url)
                if k not in seen:
                    seen.add(k)
                    d = {"url": url}
                    if isinstance(title, str) and title.strip():
                        d["title"] = title.strip()
                    out.append(d)
            continue
        if isinstance(item, dict):
            url = item.get("url") or item.get("href") or item.get("link")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            k = normalize_url(url)
            if k in seen:
                continue
            seen.add(k)
            d = {"url": url}
            title = item.get("title") or item.get("name") or item.get("label")
            if isinstance(title, str) and title.strip():
                d["title"] = title.strip()
            desc = item.get("description") or item.get("snippet") or item.get("content")
            if isinstance(desc, str) and desc.strip():
                d["description"] = desc.strip()
            out.append(d)
    return out


def _extract_links(text: str) -> list[dict]:
    sources: list[dict] = []
    seen: set[str] = set()
    for title, url in _MD_LINK.findall(text or ""):
        url = (url or "").strip()
        if not url:
            continue
        k = normalize_url(url)
        if k in seen:
            continue
        seen.add(k)
        title = (title or "").strip()
        sources.append({"title": title, "url": url} if title else {"url": url})
    for url in extract_urls(text or ""):
        k = normalize_url(url)
        if k in seen:
            continue
        seen.add(k)
        sources.append({"url": url})
    return sources
