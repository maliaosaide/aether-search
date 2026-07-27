"""FastMCP entrypoint: dual-channel search (grok primary + LLM-orchestrated aux)."""

import asyncio
import time
from typing import Literal

from fastmcp import FastMCP

from .agent import AuxAgent
from .config import mask_key, settings
from .connectors import Source
from .grok_provider import GrokProvider
from .verify import DEAD, extract_urls, mark, verify_urls

mcp = FastMCP("aether-search")

_grok = GrokProvider()


def _format_aux(sources: list[Source], limit: int = 15) -> str:
    lines = []
    for i, s in enumerate(sources[:limit], 1):
        lines.append(f"{i}. **[{s.title or s.url}]({s.url})** `{s.connector}`")
        if s.content:
            lines.append(f"   {s.content[:280]}")
    return "\n".join(lines) if lines else "(no results)"


async def _run_grok(query: str, platform: str) -> tuple[str, str | None, float]:
    start = time.monotonic()
    try:
        answer = await asyncio.wait_for(_grok.search(query, platform), timeout=settings.grok_timeout)
        return answer, None if answer else "empty response", time.monotonic() - start
    except Exception as e:
        return "", f"{type(e).__name__}: {e}", time.monotonic() - start


async def _run_aux(agent: AuxAgent, query: str) -> tuple[list[Source], dict, float]:
    start = time.monotonic()
    try:
        sources, stats = await asyncio.wait_for(agent.run(query), timeout=settings.aux_timeout)
        return sources, stats, time.monotonic() - start
    except Exception as e:
        return [], {"errors": [f"{type(e).__name__}: {e}"], "connectors": [], "variants": []}, time.monotonic() - start


@mcp.tool()
async def search(
    query: str,
    mode: Literal["full", "grok", "aux"] = "full",
    platform: str = "",
) -> str:
    """Dual-channel web search. Grok answers with citations (primary) while a
    configured LLM rewrites the query and fans it out across configured search
    APIs concurrently; results are RRF-fused and grok-cited URLs get
    liveness/corroboration marks.

    Args:
        query: The search query.
        mode: full = both channels (default); grok / aux = single channel.
        platform: Optional platforms for grok to focus on, e.g. "GitHub, Reddit".
    """
    agent = AuxAgent()

    grok_enabled = mode in ("full", "grok") and _grok.is_configured()
    aux_enabled = mode in ("full", "aux") and bool(agent.connectors)

    if not grok_enabled and not aux_enabled:
        return "Error: no channel available. Configure AETHER_GROK_API_URL/KEY and/or aux backend keys."

    grok_task = asyncio.create_task(_run_grok(query, platform)) if grok_enabled else None
    aux_task = asyncio.create_task(_run_aux(agent, query)) if aux_enabled else None

    answer, grok_err, grok_t = (await grok_task) if grok_task else ("", "channel off", 0.0)
    sources, stats, aux_t = (await aux_task) if aux_task else ([], {}, 0.0)

    out: list[str] = []

    # Primary body: grok answer, or aux promoted when grok failed.
    if answer:
        out.append(answer)
        urls = extract_urls(answer)
        if urls and aux_enabled:
            verdicts = await verify_urls(urls, [s.url for s in sources])
            live = [(u, *verdicts[u]) for u in urls if verdicts[u][0] != DEAD]
            dead = [(u, *verdicts[u]) for u in urls if verdicts[u][0] == DEAD]
            out.append("\n## Grok 引用核验\n")
            for url, status, detail in live + dead:
                out.append(f"- {mark(status)} {url} ({detail})")
    elif grok_enabled:
        out.append(f"> ⚠ Grok 通道失败（{grok_err}），以下为辅助检索结果。\n")

    if sources:
        digest = await agent.digest(query, sources)
        out.append("\n## 辅助检索（多后端 RRF 融合）\n")
        if digest:
            out.append(digest + "\n")
        out.append(_format_aux(sources))

    status = []
    if grok_enabled:
        status.append(f"grok: {'ok' if answer else f'FAIL({grok_err})'} {grok_t:.1f}s")
    if aux_enabled:
        parts = f"aux: {stats.get('hits', 0)} hits via {','.join(stats.get('connectors', []))} {aux_t:.1f}s"
        if len(stats.get("variants", [])) > 1:
            parts += f", {len(stats['variants'])} queries"
        if stats.get("errors"):
            parts += f", errors: {'; '.join(stats['errors'][:3])}"
        status.append(parts)
    out.append(f"\n---\n*{' | '.join(status)}*")

    return "\n".join(out)


@mcp.tool()
async def get_config() -> str:
    """Show aether-search configuration status (keys masked)."""
    agent = AuxAgent()
    lines = [
        f"grok: {settings.grok_api_url or '(not set)'} model={settings.grok_model} key={mask_key(settings.grok_api_key)}",
        f"aux LLM: {settings.llm_api_base or '(default OpenAI)'} model={settings.llm_model or '(not set)'} "
        f"key={mask_key(settings.llm_api_key)} rewrite={settings.llm_rewrite} digest={settings.llm_digest}",
        f"aux backends: {', '.join(c.name for c in agent.connectors) or '(none)'}",
        f"fanout={settings.fanout} variants={settings.max_variants} top_k={settings.top_k} "
        f"concurrency={settings.max_concurrency} verify={settings.verify}",
    ]
    return "\n".join(lines)


def main():
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
