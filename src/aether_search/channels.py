"""Agent 答案通道；Grok 实现和共享结果类型由独立模块提供。"""

import asyncio
import json
import time

from . import fetcher
from .agent import DIGEST_SYSTEM, AuxAgent
from .channel_types import AnswerResult
from .config import settings
from .connectors.base import Source
from .grok import GrokChannel
from .llm import ToolsNotSupported
from .verify import extract_urls, normalize_url

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
