"""FastMCP entrypoint: dual-channel search (grok primary + LLM-orchestrated aux).

Two channels run concurrently; verification runs as three parallel tasks after both
return — any LLM text (grok answer, aux digest) gets full verify (corroboration +
probe), any URL surfaced to the user (aux links) gets a liveness probe. The assembled
output carries each channel's product and its verdicts so the caller can compare.
"""

import asyncio
import time
from typing import Literal

from fastmcp import FastMCP

from .agent import AuxAgent
from .config import mask_key, settings
from .connectors import Source
from .grok_provider import GrokProvider
from .verify import DEAD, extract_urls, mark, probe_urls, verify_urls

mcp = FastMCP("aether-search")

_grok = GrokProvider()


def _format_aux(sources: list[Source], limit: int = 15, verdicts: dict | None = None) -> str:
    lines = []
    for i, s in enumerate(sources[:limit], 1):
        v = (verdicts or {}).get(s.url)
        tag = f" {mark(v[0])}" if v else ""
        lines.append(f"{i}. **[{s.title or s.url}]({s.url})** `{s.connector}`{tag}")
        if s.content:
            lines.append(f"   {s.content[:280]}")
    return "\n".join(lines) if lines else "(no results)"


def _verification_block(title: str, urls: list[str], verdicts: dict) -> list[str]:
    """Compact ✓/⚠ list for an LLM-cited URL set; dead links sink to the bottom."""
    if not verdicts:
        return []
    live = [(u, *verdicts[u]) for u in urls if u in verdicts and verdicts[u][0] != DEAD]
    dead = [(u, *verdicts[u]) for u in urls if u in verdicts and verdicts[u][0] == DEAD]
    if not (live or dead):
        return []
    lines = [f"\n{title}\n"]
    for url, status, detail in live + dead:
        lines.append(f"- {mark(status)} {url} ({detail})")
    return lines


async def _run_grok(query: str, platform: str) -> tuple[str, str | None, float]:
    start = time.monotonic()
    try:
        answer = await asyncio.wait_for(_grok.search(query, platform), timeout=settings.grok_timeout)
        return answer, None if answer else "empty response", time.monotonic() - start
    except Exception as e:
        return "", f"{type(e).__name__}: {e}", time.monotonic() - start


async def _run_aux(agent: AuxAgent, query: str) -> tuple[list[Source], str, dict, float]:
    start = time.monotonic()
    try:
        async def _pipeline():
            sources, stats = await agent.run(query)
            digest = await agent.digest(query, sources)
            return sources, digest, stats

        sources, digest, stats = await asyncio.wait_for(_pipeline(), timeout=settings.aux_timeout)
        return sources, digest, stats, time.monotonic() - start
    except Exception as e:
        return [], "", {"errors": [f"{type(e).__name__}: {e}"], "connectors": [], "variants": []}, time.monotonic() - start


@mcp.tool()
async def search(
    query: str,
    mode: Literal["full", "grok", "aux"] = "full",
    platform: str = "",
) -> str:
    """Dual-channel web search. Grok answers with citations (primary) while a
    configured LLM rewrites the query and fans it out across configured search
    APIs concurrently; results are RRF-fused and grok-cited URLs get liveness/corroboration
    marks. Both channels and all verification tasks run concurrently; the output
    carries each channel's product and verdicts for the caller to compare.

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

    # Stage 1 — two channels in parallel (aux pipeline includes digest).
    grok_task = asyncio.create_task(_run_grok(query, platform)) if grok_enabled else None
    aux_task = asyncio.create_task(_run_aux(agent, query)) if aux_enabled else None

    answer, grok_err, grok_t = (await grok_task) if grok_task else ("", "channel off", 0.0)
    sources, digest, stats, aux_t = (await aux_task) if aux_task else ([], "", {}, 0.0)

    # Stage 2 — verification as three parallel tasks.
    grok_urls = extract_urls(answer) if answer else []
    digest_urls = extract_urls(digest) if digest else []
    aux_urls = [s.url for s in sources]
    aux_display_urls = [s.url for s in sources[:15]]

    vtasks: dict[str, asyncio.Future] = {}
    if settings.verify:
        if grok_urls:
            vtasks["grok"] = asyncio.ensure_future(verify_urls(grok_urls, aux_urls))
        if digest_urls:
            vtasks["digest"] = asyncio.ensure_future(verify_urls(digest_urls, aux_urls))
        if aux_display_urls:
            vtasks["aux"] = asyncio.ensure_future(probe_urls(aux_display_urls))

    verdicts: dict[str, dict] = {}
    if vtasks:
        keys = list(vtasks.keys())
        results = await asyncio.gather(*vtasks.values(), return_exceptions=True)
        for k, r in zip(keys, results):
            verdicts[k] = r if isinstance(r, dict) else {}

    grok_v = verdicts.get("grok", {})
    digest_v = verdicts.get("digest", {})
    aux_v = verdicts.get("aux", {})

    # Stage 3 — assemble both channels, each with its own verdicts.
    out: list[str] = []

    if answer:
        out.append(answer)
        out.extend(_verification_block("## Grok 引用核验", grok_urls, grok_v))
    elif grok_enabled:
        out.append(f"> ⚠ Grok 通道失败（{grok_err}），以下为辅助检索结果。\n")

    if sources:
        out.append("\n## 辅助检索（多后端 RRF 融合）\n")
        if digest:
            out.append(digest + "\n")
            out.extend(_verification_block("*Aux 摘要引用核验：*", digest_urls, digest_v))
        out.append(_format_aux(sources, verdicts=aux_v))

    status = []
    if grok_enabled:
        status.append(f"grok: {'ok' if answer else f'FAIL({grok_err})'} {grok_t:.1f}s")
    if aux_enabled:
        parts = f"aux: {stats.get('hits', 0)} hits via {','.join(stats.get('connectors', []))} {aux_t:.1f}s"
        if len(stats.get("variants", [])) > 1:
            parts += f", {len(stats['variants'])} queries"
        if digest:
            parts += ", digest✓"
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
