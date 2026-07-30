"""Dual answer channels: GrokChannel (LLM-native web search, multi-endpoint) + AgentChannel
(LLM tool-calling loop over aux backends). Both produce AnswerResult; agent channel also
emits evidence used for cross-corroboration and fallback.

GrokChannel ports grok_provider.py (SSE + non-stream fallback, Retry-After, xAI Live Search,
OpenRouter :online, time injection) and adds multi-endpoint sequential/race with empty-body
failover. AgentChannel replaces the plan×6 scaffolding with an implicit RESEARCH_SYSTEM.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential
from tenacity.wait import wait_base

from . import fetcher
from .agent import DIGEST_SYSTEM, AuxAgent
from .config import settings
from .connectors.base import Source
from .llm import ToolsNotSupported
from .verify import extract_urls, normalize_url

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

GROK_SEARCH_PROMPT = """\
You are a web research agent. Search the web thoroughly before answering.

1. Infer the user's true intent; brainstorm multiple angles and run parallel searches.
2. Verify every claim; prioritize authoritative sources (official docs, academic, reputable media).
3. Search in English first for volume/quality; switch to Chinese when the topic demands.
4. Every claim must carry its source URL inline. No citation, no claim.
5. Output polished Markdown, most probable answer first. End with a "Sources:" list of all cited URLs, one per line.
"""

RESEARCH_SYSTEM = """\
You are a web research agent. Initial search results are provided; use them and decide \
whether more searching or page fetches are needed before answering.

Workflow:
1. Assess whether the initial results answer the query. If gaps remain, call search_multi with \
refined queries from new angles (English first for volume). Call fetch_page only when a result's \
snippet is insufficient and the full page is needed.
2. Stop searching once evidence is enough — do not over-search.
3. Every claim must cite its source URL inline as [N], matching the Sources list.

Rules:
- State only what the evidence supports. If evidence is insufficient, say so explicitly; never \
fabricate URLs or facts.
- Output polished Markdown, most probable answer first.
- End with a "Sources:" list of all cited URLs, one per line.
"""

SEARCH_MULTI_SPEC = {"type": "function", "function": {
    "name": "search_multi",
    "description": "Fan out across configured search backends + RRF fusion. Returns an evidence block (title/url/snippet/backend).",
    "parameters": {"type": "object",
                   "properties": {"query": {"type": "string", "description": "Search query"}},
                   "required": ["query"]},
}}

FETCH_PAGE_SPEC = {"type": "function", "function": {
    "name": "fetch_page",
    "description": "Fetch a URL and return its content as markdown (Tavily Extract -> Firecrawl Scrape).",
    "parameters": {"type": "object",
                   "properties": {"url": {"type": "string", "description": "URL to fetch"}},
                   "required": ["url"]},
}}


@dataclass
class Endpoint:
    url: str
    key: str
    model: str = ""
    label: str = ""


@dataclass
class AnswerResult:
    channel: str                 # "grok" | "agent"
    text: str
    citations: list[str]
    evidence: list[Source]       # agent channel output; grok channel is []
    detail: str
    elapsed: float
    error: str | None            # None|"empty"|"timeout"|"http_status"|"parse_fail"|"no_endpoint"|"tools_unsupported"|"no_backend"|"max_steps"


def _time_context() -> str:
    now = datetime.now().astimezone()
    return f"[Current time: {now.strftime('%Y-%m-%d %H:%M %Z')}]\n"


def _is_retryable(exc) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in RETRYABLE_STATUS


class _WaitRetryAfter(wait_base):
    def __init__(self):
        self._base = wait_random_exponential(multiplier=1, max=10)

    def __call__(self, retry_state):
        if retry_state.outcome and retry_state.outcome.failed:
            exc = retry_state.outcome.exception()
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                header = (exc.response.headers.get("Retry-After") or "").strip()
                if header.isdigit():
                    return float(header)
                try:
                    dt = parsedate_to_datetime(header)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError):
                    pass
        return self._base(retry_state)


def _format_evidence(sources: list[Source], limit: int = 20) -> str:
    if not sources:
        return "(no results)"
    lines = []
    for i, s in enumerate(sources[:limit], 1):
        lines.append(f"[{i}] {s.title or s.url} — {s.url} ({s.connector})")
        if s.content:
            lines.append(f"    {s.content[:280]}")
    return "\n".join(lines)


def _tool_result(call, text: str) -> dict:
    return {"role": "tool", "tool_call_id": call.id, "content": text}


class GrokChannel:
    """LLM-native web search over one or more OpenAI-compatible endpoints.

    Pure LLM联网 search — never calls any external /search API (keeps it an independent
    source vs the agent channel's backend retrieval). Per-endpoint failure = exception OR
    empty body (empty body does not raise, must be checked explicitly).
    """

    def __init__(self):
        self.endpoints = self._parse_endpoints(settings.grok_endpoints)

    @staticmethod
    def _parse_endpoints(raw: str) -> list[Endpoint]:
        raw = (raw or "").strip()
        if not raw:
            if settings.grok_api_url and settings.grok_api_key:
                return [Endpoint(settings.grok_api_url, settings.grok_api_key, settings.grok_model)]
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        out = []
        for e in data or []:
            if isinstance(e, dict) and e.get("url") and e.get("key"):
                out.append(Endpoint(e["url"], e["key"], e.get("model") or settings.grok_model, e.get("label", "")))
        return out

    def enabled(self) -> bool:
        return len(self.endpoints) > 0

    @staticmethod
    def _effective_model(endpoint: Endpoint) -> str:
        model = endpoint.model or settings.grok_model
        if "openrouter" in endpoint.url and ":online" not in model:
            return f"{model}:online"
        return model

    async def _search_one(self, endpoint: Endpoint, query: str, platform: str) -> str:
        """Single-endpoint search with tenacity retry. Raises on failure; may return empty body."""
        user = _time_context() + query
        if platform:
            user += f"\n\nFocus your web search on these platforms: {platform}"
        payload = {
            "model": self._effective_model(endpoint),
            "messages": [
                {"role": "system", "content": GROK_SEARCH_PROMPT},
                {"role": "user", "content": user},
            ],
            "stream": True,
        }
        mode = settings.grok_search_mode
        if not mode:
            mode = "auto" if "x.ai" in endpoint.url else "none"
        if mode in ("auto", "on", "off"):
            payload["search_parameters"] = {"mode": mode, "return_citations": True}
        headers = {"Authorization": f"Bearer {endpoint.key}", "Content-Type": "application/json"}
        timeout = httpx.Timeout(connect=6.0, read=settings.grok_timeout, write=10.0, pool=None)

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(settings.retry_max_attempts + 1),
                wait=_WaitRetryAfter(),
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    async with client.stream(
                        "POST", f"{endpoint.url}/chat/completions", headers=headers, json=payload,
                    ) as response:
                        response.raise_for_status()
                        return await self._parse_stream(response)
        return ""

    @staticmethod
    async def _parse_stream(response) -> str:
        content = ""
        buffer = []
        async for line in response.aiter_lines():
            line = line.strip()
            if not line:
                continue
            buffer.append(line)
            if line.startswith("data:"):
                if line in ("data: [DONE]", "data:[DONE]"):
                    continue
                try:
                    data = json.loads(line[5:].lstrip())
                    choices = data.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        if "content" in delta:
                            content += delta["content"]
                except (json.JSONDecodeError, IndexError):
                    continue
        if not content and buffer:
            try:
                data = json.loads("".join(buffer))
                if data.get("choices"):
                    content = data["choices"][0].get("message", {}).get("content", "")
            except json.JSONDecodeError:
                pass
        return content

    async def _sequential(self, query: str, platform: str) -> AnswerResult:
        start = time.monotonic()
        err = "no_endpoint"
        for ep in self.endpoints:
            try:
                content = await self._search_one(ep, query, platform)
            except Exception as e:
                err = f"http_status:{type(e).__name__}"
                continue
            if not content.strip():
                err = "empty"
                continue
            return AnswerResult("grok", content, extract_urls(content), [],
                                ep.label or ep.url, time.monotonic() - start, None)
        return AnswerResult("grok", "", [], [], "", time.monotonic() - start, err)

    async def _race(self, query: str, platform: str) -> AnswerResult:
        start = time.monotonic()

        async def _one(ep: Endpoint) -> tuple[Endpoint, str]:
            try:
                return ep, await self._search_one(ep, query, platform)
            except Exception:
                return ep, ""

        tasks = [asyncio.create_task(_one(ep)) for ep in self.endpoints]
        try:
            for coro in asyncio.as_completed(tasks):
                ep, content = await coro
                if content.strip():
                    return AnswerResult("grok", content, extract_urls(content), [],
                                        ep.label or ep.url, time.monotonic() - start, None)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
        return AnswerResult("grok", "", [], [], "", time.monotonic() - start, "empty")

    async def answer(self, query: str, platform: str = "") -> AnswerResult:
        if not self.endpoints:
            return AnswerResult("grok", "", [], [], "", 0.0, "no_endpoint")
        runner = self._race if settings.grok_failover == "race" else self._sequential
        try:
            return await asyncio.wait_for(runner(query, platform), timeout=settings.grok_timeout * (len(self.endpoints) + 1))
        except asyncio.TimeoutError:
            return AnswerResult("grok", "", [], [], "", settings.grok_timeout, "timeout")


class AgentChannel:
    """LLM tool-calling loop over aux backends. Seed search runs unconditionally (evidence is
    never empty when backends are configured); the LLM loop above it is an optional upgrade
    that requires an Aux LLM. Has per-instance _evidence state — instantiate per search.
    """

    def __init__(self):
        self.agent = AuxAgent()
        self.llm = self.agent.llm
        self._evidence: dict[str, Source] = {}

    def enabled(self) -> bool:
        return bool(self.agent.primary_connectors)

    def _add_evidence(self, sources):
        for s in sources:
            k = normalize_url(s.url)
            if k and k not in self._evidence:
                self._evidence[k] = s

    @property
    def evidence(self) -> list[Source]:
        return list(self._evidence.values())

    async def answer(self, query: str, platform: str = "") -> AnswerResult:
        start = time.monotonic()
        self._evidence.clear()
        if not self.agent.primary_connectors:
            return AnswerResult("agent", "", [], [], "no backend", time.monotonic() - start, "no_backend")
        try:
            return await asyncio.wait_for(self._run(query, platform, start), timeout=settings.agent_timeout)
        except asyncio.TimeoutError:
            return await self._fallback(query, start, "timeout")

    async def _run(self, query: str, platform: str, start: float) -> AnswerResult:
        sources, _ = await self.agent.run_queries([query])
        self._add_evidence(sources)
        if not self.llm.is_configured():
            return self._evidence_answer(start, "evidence only")
        hint = f"\nFocus platforms: {platform}" if platform else ""
        user = query + hint + "\n\n[Initial results]\n" + _format_evidence(self.evidence)
        msgs = [{"role": "system", "content": RESEARCH_SYSTEM}, {"role": "user", "content": user}]
        tools = [SEARCH_MULTI_SPEC] + ([FETCH_PAGE_SPEC] if fetcher.configured() else [])
        try:
            for step in range(settings.agent_max_steps):
                msg = await self.llm.complete_with_tools(msgs, tools)
                msgs.append(msg.raw)
                if not msg.tool_calls:
                    return AnswerResult("agent", msg.content, extract_urls(msg.content),
                                        self.evidence, f"{step + 1} steps", time.monotonic() - start, None)
                results = await asyncio.gather(*(self._exec_tool(c) for c in msg.tool_calls))
                for call, (text, new_src) in zip(msg.tool_calls, results):
                    self._add_evidence(new_src)
                    msgs.append(_tool_result(call, text))
            return await self._fallback(query, start, "max_steps")
        except ToolsNotSupported:
            return await self._fallback(query, start, "tools_unsupported")
        except Exception as e:
            return await self._fallback(query, start, f"llm_error:{type(e).__name__}")

    async def _digest(self, query: str, sources: list[Source]) -> str:
        """LLM digest of accumulated evidence — the agent channel's fallback answer path."""
        if not (self.llm.is_configured() and sources):
            return ""
        numbered = "\n".join(
            f"[{i}] {s.title} — {s.url}\n{s.content[:300]}" for i, s in enumerate(sources[:10], 1)
        )
        try:
            return await self.llm.complete(DIGEST_SYSTEM, f"Query: {query}\n\n{numbered}", max_tokens=400)
        except Exception:
            return ""

    async def _fallback(self, query: str, start: float, reason: str) -> AnswerResult:
        """ToolsNotSupported / timeout / max_steps: LLM→digest, else raw evidence. Never empty output."""
        if self.llm.is_configured() and self.evidence:
            text = await self._digest(query, self.evidence) or _format_evidence(self.evidence)
        else:
            text = _format_evidence(self.evidence)
        return AnswerResult("agent", text, extract_urls(text), self.evidence,
                            f"forced({reason})", time.monotonic() - start, reason)

    def _evidence_answer(self, start: float, detail: str) -> AnswerResult:
        text = _format_evidence(self.evidence)
        return AnswerResult("agent", text, extract_urls(text), self.evidence, detail, time.monotonic() - start, None)

    async def _exec_tool(self, call) -> tuple[str, list[Source]]:
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        name = call.function.name
        if name == "search_multi":
            src, _ = await self.agent.run_queries([args.get("query", "")])
            return _format_evidence(src), src
        if name == "fetch_page":
            return await fetcher.fetch(args.get("url", "")), []
        return "unknown tool", []
