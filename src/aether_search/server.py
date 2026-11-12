"""FastMCP entrypoint: dual-channel search (grok primary + agent secondary).

Two channels run concurrently if configured ("configure what you use"). After both return,
citations are cross-corroborated against the agent evidence pool and probed; output is
assembled with grok as primary, agent taking over when grok has no body, evidence links as
last resort. Sources (citations + evidence) are deduped into a session cache.
"""

import asyncio
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from fastmcp import FastMCP

from . import fetcher
from .channels import AgentChannel, GrokChannel
from .config import mask_key, settings
from .connectors.base import ImageResult, ImageSearchResult, Source
from .grok_protocol import markdown_safe_url
from .sources import SourcesCache, merge_sources, new_session_id, source_to_dict, split_answer_and_sources
from .verify import DEAD, extract_urls, mark, probe_urls, verify_urls

mcp = FastMCP("aether-search")

_SOURCES_CACHE = SourcesCache(max_size=settings.sources_cache_size)


def _citations_and_sources(text: str) -> tuple[list[str], list[dict]]:
    if not text:
        return [], []
    _, srcs = split_answer_and_sources(text)
    citations = [s["url"] for s in srcs] or extract_urls(text)
    return citations, srcs


def _format_aux(sources: list[Source], verdicts: dict, limit: int = 15) -> str:
    lines = []
    for i, s in enumerate(sources[:limit], 1):
        v = verdicts.get(s.url)
        tag = f" {mark(v[0])}" if v else ""
        lines.append(f"{i}. **[{s.title or s.url}]({s.url})** `{s.connector}`{tag}")
        if s.content:
            lines.append(f"   {s.content[:280]}")
    return "\n".join(lines) if lines else "(no results)"


def _verification_block(title: str, urls: list[str], verdicts: dict) -> list[str]:
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


def _merge_verdicts(verdicts: dict[str, dict]) -> dict:
    merged: dict[str, tuple] = {}
    for v in verdicts.values():
        if isinstance(v, dict):
            merged.update(v)
    return merged


def _merge_images(results: list[ImageSearchResult], limit: int) -> list[ImageResult]:
    """Round-robin merge so no single backend dominates, deduped by image URL."""
    queues = [list(r.images) for r in results if r.images]
    merged: list[ImageResult] = []
    seen: set[str] = set()
    while len(merged) < limit and any(queues):
        for queue in queues:
            while queue:
                image = queue.pop(0)
                if isinstance(image.image_url, str) and image.image_url and image.image_url not in seen:
                    seen.add(image.image_url)
                    merged.append(image)
                    break
            if len(merged) >= limit:
                break
    return merged


def _annotate_sources(sources: list[dict], merged: dict) -> list[dict]:
    out = []
    for s in sources:
        v = merged.get(s.get("url"))
        out.append({**s, "verify": mark(v[0]), "verify_detail": v[1]} if v else s)
    return out


@mcp.tool()
async def search(query: str, platform: str = "", sources_mode: str = "inline") -> str:
    """Dual-channel web search. Grok answers with citations (primary); the agent channel
    retrieves across configured backends and, when an Aux LLM is configured, runs a
    tool-calling loop to produce a cited answer. Citations are cross-corroborated against
    the agent evidence pool and probed. "Configure what you use": each channel runs only
    if its prerequisites are set.

    Args:
        query: The search query.
        platform: Optional platforms for grok to focus on, e.g. "GitHub, Reddit".
        sources_mode: "inline" (default, full output + session_id) | "session" (answer +
            verification only; retrieve sources via get_sources).
    """
    grok_ch, agent_ch = GrokChannel(), AgentChannel()
    grok_enabled, agent_enabled = grok_ch.enabled(), agent_ch.enabled()
    if not (grok_enabled or agent_enabled):
        return ("Error: 配置不足。配 grok 端点（AETHER_GROK_API_URL/KEY 或 AETHER_GROK_ENDPOINTS）"
                "和/或至少一个搜索后端 key。")

    coros = []
    if grok_enabled:
        coros.append(grok_ch.answer(query, platform))
    if agent_enabled:
        coros.append(agent_ch.answer(query, platform))
    outs = await asyncio.gather(*coros)
    idx = 0
    grok_r = outs[idx] if grok_enabled else None
    if grok_enabled:
        idx += 1
    agent_r = outs[idx] if agent_enabled else None

    grok_cit, grok_src = _citations_and_sources(grok_r.text if grok_r else "")
    agent_cit, agent_src = _citations_and_sources(agent_r.text if agent_r else "")
    evidence_urls = [s.url for s in (agent_r.evidence if agent_r else [])]

    verdicts: dict[str, dict] = {}
    if settings.verify:
        vtasks: dict[str, asyncio.Future] = {}
        if grok_cit:
            vtasks["grok"] = asyncio.ensure_future(verify_urls(grok_cit, set(evidence_urls + agent_cit)))
        if agent_cit:
            vtasks["agent"] = asyncio.ensure_future(verify_urls(agent_cit, set(evidence_urls + grok_cit)))
        if evidence_urls[:15]:
            vtasks["evidence"] = asyncio.ensure_future(probe_urls(evidence_urls[:15]))
        if vtasks:
            keys = list(vtasks)
            results = await asyncio.gather(*vtasks.values(), return_exceptions=True)
            for k, r in zip(keys, results):
                verdicts[k] = r if isinstance(r, dict) else {}

    merged_v = _merge_verdicts(verdicts)

    out: list[str] = []
    if grok_r and grok_r.text:
        out.append(grok_r.text)
        out.extend(_verification_block("## Grok 引用核验", grok_cit, verdicts.get("grok", {})))
        primary = "grok"
    elif agent_r and agent_r.text:
        out.append(agent_r.text)
        out.extend(_verification_block("## Agent 引用核验", agent_cit, verdicts.get("agent", {})))
        primary = "agent"
    else:
        primary = None

    show_evidence = (sources_mode != "session") and agent_r and agent_r.evidence
    if primary == "grok" and show_evidence:
        out.append("\n## 辅助证据（多后端 RRF 融合）\n")
        out.append(_format_aux(agent_r.evidence, verdicts.get("evidence", {})))

    if not primary:
        if agent_r and agent_r.evidence:
            out.append("> ⚠ 双答案通道均失败，以下为检索证据。\n")
            out.append(_format_aux(agent_r.evidence, verdicts.get("evidence", {})))
        else:
            out.append("> ⚠ 所有通道失败且无证据，请检查 grok 端点与搜索后端配置。")

    status = []
    if grok_enabled and grok_r:
        status.append(f"grok: {'ok' if grok_r.text else f'FAIL({grok_r.error})'} {grok_r.elapsed:.1f}s {grok_r.detail}".strip())
    if agent_enabled and agent_r:
        status.append(f"agent: {agent_r.detail}, {len(agent_r.evidence)} evidence {agent_r.elapsed:.1f}s")

    if agent_r and len(agent_r.evidence) < settings.top_k and agent_ch.agent.fallback_connectors:
        fb_names = ",".join(c.name for c in agent_ch.agent.fallback_connectors)
        out.append(f"\n💡 主层结果偏少（{len(agent_r.evidence)} 条）。如需深入，可调用 search_deep(query) 走补充层（{fb_names}）。")

    session_id = new_session_id()
    evidence_dicts = [source_to_dict(s) for s in (agent_r.evidence if agent_r else [])]
    all_sources = merge_sources(grok_src, agent_src, evidence_dicts)
    await _SOURCES_CACHE.set(session_id, _annotate_sources(all_sources, merged_v))

    out.append(f"\n---\n*{' | '.join(status)}*\n*session_id: {session_id}*")
    return "\n".join(out)


@mcp.tool()
async def search_deep(query: str, platform: str = "") -> str:
    """Deep supplementary search over fallback backends (sonar/linkup). Call when search()
    returned too few primary-layer results. Returns RRF-fused evidence with verify marks + session_id."""
    from .agent import AuxAgent
    agent = AuxAgent()
    if not agent.fallback_connectors:
        return "Error: 未配置补充层后端。在 AETHER_FALLBACK_BACKENDS 指定且对应后端 key 已配。"
    queries = await agent.rewrite(query)
    sources, stats = await agent.run_fallback(queries)
    if not sources:
        return f"补充层无结果。errors: {stats.get('errors')[:3] or 'none'}"

    verdicts: dict[str, dict] = {}
    evidence_urls = [s.url for s in sources]
    if settings.verify and evidence_urls[:15]:
        verdicts["evidence"] = await probe_urls(evidence_urls[:15])
    merged_v = _merge_verdicts(verdicts)

    out = [_format_aux(sources, verdicts.get("evidence", {}))]
    session_id = new_session_id()
    await _SOURCES_CACHE.set(session_id, _annotate_sources([source_to_dict(s) for s in sources], merged_v))

    out.append(f"\n---\n*deep: {stats.get('hits', 0)} hits via {','.join(stats.get('connectors', []))}*\n*session_id: {session_id}*")
    return "\n".join(out)


@mcp.tool()
async def search_images(query: str, count: int = 10) -> str:
    """Search the web for images across every configured image-capable backend
    (serper/brave/parallel/tavily/firecrawl/linkup). Returns direct image URLs with
    titles, source pages and dimensions, round-robin merged and deduplicated.

    Use this when the user asks to find, show, collect or look up pictures, photos,
    illustrations, logos, wallpapers or image assets (Chinese triggers: 找图 / 配图 /
    图片素材 / 搜图片). Do not use it to read or describe an image that already has
    a known URL.

    Args:
        query: What the images should depict, e.g. "golden gate bridge at sunset".
        count: Maximum number of merged results to return (1-50, default 10). Each
            configured backend is asked for up to this many before deduplication.
    """
    query = query.strip()
    if not query:
        return "Error: query 不能为空。"
    from .connectors import active_connectors
    connectors = [c for c in active_connectors() if c.supports_images]
    if not connectors:
        return ("Error: 未配置支持图片检索的后端。配置以下任一 key 即可："
                "AETHER_SERPER_API_KEY / AETHER_BRAVE_API_KEY / AETHER_PARALLEL_API_KEY / "
                "AETHER_TAVILY_API_KEY / AETHER_FIRECRAWL_API_KEY / AETHER_LINKUP_API_KEY。")

    count = max(1, min(count, 50))
    sem = asyncio.Semaphore(max(1, settings.max_concurrency))

    async def _run(connector):
        async with sem:
            return await connector.search_images(query, count)

    raw = await asyncio.gather(*(_run(c) for c in connectors), return_exceptions=True)
    results = [
        r if isinstance(r, ImageSearchResult)
        else ImageSearchResult(images=[], query=query, connector_name=c.name, error=str(r))
        for c, r in zip(connectors, raw)
    ]
    images = _merge_images(results, count)
    if not images:
        errors = " | ".join(f"{r.connector_name}: {r.error}" for r in results if r.error)
        return f"> 未找到图片。错误: {errors}" if errors else "> 未找到图片。"

    lines = [f"## 图片搜索结果：{query}\n"]
    for i, img in enumerate(images, 1):
        raw_title = img.title if isinstance(img.title, str) else ""
        description = img.description if isinstance(img.description, str) else ""
        title = " ".join((raw_title or description[:80] or "image").split())
        title = title.replace("[", "(").replace("]", ")")
        meta = [f"`{img.connector}`"]
        if img.width and img.height:
            meta.append(f"{img.width}×{img.height}")
        if isinstance(img.source_url, str) and img.source_url:
            meta.append(f"[来源页]({markdown_safe_url(img.source_url)})")
        lines.append(f"{i}. **{title}** {' · '.join(meta)}")
        lines.append(f"   ![{title}]({markdown_safe_url(img.image_url)})")
        lines.append(f"   {img.image_url}")
        if isinstance(img.thumbnail_url, str) and img.thumbnail_url and img.thumbnail_url != img.image_url:
            lines.append(f"   缩略图: {img.thumbnail_url}")
    lines.append("\n---")
    lines.append("*backends: " + ", ".join(
        f"{r.connector_name}({len(r.images)})" for r in results) + "*")
    errors = [f"{r.connector_name}: {r.error}" for r in results if r.error]
    if errors:
        lines.append("*errors: " + " | ".join(errors) + "*")
    return "\n".join(lines)


@mcp.tool()
async def get_sources(session_id: str) -> str:
    """Retrieve all cached sources (with verify marks) for a previous search call."""
    sources = await _SOURCES_CACHE.get(session_id)
    if sources is None:
        return json.dumps({"session_id": session_id, "sources": [], "sources_count": 0,
                           "error": "session_id_not_found_or_expired"}, ensure_ascii=False, indent=2)
    return json.dumps({"session_id": session_id, "sources": sources,
                       "sources_count": len(sources)}, ensure_ascii=False, indent=2)


@mcp.tool()
async def web_fetch(url: str) -> str:
    """Fetch a URL as Markdown (DeepSeek Fetch -> Tavily Extract -> Firecrawl Scrape)."""
    return await fetcher.fetch(url)


@mcp.tool()
async def web_map(
    url: str,
    instructions: str = "",
    max_depth: int = 1,
    max_breadth: int = 20,
    limit: int = 50,
    timeout: int = 150,
) -> str:
    """Map a website's structure (Tavily Map)."""
    return await fetcher.tavily_map(url, instructions, max_depth, max_breadth, limit, timeout)


@mcp.tool()
async def toggle_builtin_tools(action: str = "status") -> str:
    """Toggle Claude Code's built-in WebSearch/WebFetch on/off in the caller project's
    .claude/settings.json. action: "on" (block built-in) / "off" (allow) / "status"."""
    root = Path.cwd()
    while root != root.parent and not (root / ".git").exists():
        root = root.parent
    settings_path = root / ".claude" / "settings.json"
    tools = ["WebFetch", "WebSearch"]

    if settings_path.exists():
        with open(settings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"permissions": {"deny": []}}
    deny = data.setdefault("permissions", {}).setdefault("deny", [])
    blocked = all(t in deny for t in tools)

    if action in ("on", "enable"):
        for t in tools:
            if t not in deny:
                deny.append(t)
        blocked = True
        msg = "官方工具已禁用"
    elif action in ("off", "disable"):
        deny[:] = [t for t in deny if t not in tools]
        blocked = False
        msg = "官方工具已启用"
    else:
        msg = f"官方工具当前{'已禁用' if blocked else '已启用'}"

    if action in ("on", "enable", "off", "disable"):
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    return json.dumps({"blocked": blocked, "deny_list": deny, "file": str(settings_path),
                       "message": msg}, ensure_ascii=False, indent=2)


async def _probe_endpoint(url: str, key: str) -> dict:
    import httpx
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{url.rstrip('/')}/models",
                                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        ms = round((time.monotonic() - start) * 1000, 1)
        if resp.status_code == 200:
            models = [m.get("id") for m in resp.json().get("data", []) if isinstance(m, dict) and m.get("id")]
            return {"url": url, "status": "ok", "ms": ms, "models": len(models)}
        return {"url": url, "status": f"HTTP {resp.status_code}", "ms": ms}
    except Exception as e:
        return {"url": url, "status": f"{type(e).__name__}", "ms": round((time.monotonic() - start) * 1000, 1)}


@mcp.tool()
async def get_config() -> str:
    """Show aether-search configuration (keys masked) + per-endpoint /models connectivity."""
    from .agent import AuxAgent
    grok = GrokChannel()
    agent = AuxAgent()
    lines = [
        f"grok endpoints: {len(grok.endpoints)}",
        f"  single: url={settings.grok_api_url or '(not set)'} model={settings.grok_model} key={mask_key(settings.grok_api_key)} failover={settings.grok_failover}",
        f"aux LLM: base={settings.llm_api_base or '(default OpenAI)'} model={settings.llm_model or '(not set)'} "
        f"key={mask_key(settings.llm_api_key)}",
        f"aux backends: {', '.join(c.name for c in agent.connectors) or '(none)'}",
        f"image backends: {', '.join(c.name for c in agent.connectors if c.supports_images) or '(none)'}",
        f"fanout={settings.fanout} variants={settings.max_variants} top_k={settings.top_k} "
        f"concurrency={settings.max_concurrency} verify={settings.verify}",
        f"agent: max_steps={settings.agent_max_steps} timeout={settings.agent_timeout}s "
        f"fetch_timeout={settings.fetch_timeout}s scrape_retry={settings.scrape_retry}",
    ]
    if grok.endpoints:
        probes = await asyncio.gather(*[_probe_endpoint(ep.url, ep.key) for ep in grok.endpoints])
        lines.append("endpoint diagnostics: " + json.dumps(probes, ensure_ascii=False))
    return "\n".join(lines)


def main():
    if threading.current_thread() is threading.main_thread():
        def handle_shutdown(signum, frame):
            os._exit(0)
        signal.signal(signal.SIGINT, handle_shutdown)
        if sys.platform != "win32":
            signal.signal(signal.SIGTERM, handle_shutdown)

    if sys.platform == "win32":
        import ctypes
        parent_pid = os.getppid()

        def is_parent_alive(pid: int) -> bool:
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return True
            exit_code = ctypes.c_ulong()
            result = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return bool(result) and exit_code.value == STILL_ACTIVE

        def monitor_parent():
            while True:
                if not is_parent_alive(parent_pid):
                    os._exit(0)
                time.sleep(2)

        threading.Thread(target=monitor_parent, daemon=True).start()

    try:
        mcp.run(show_banner=False)
    except KeyboardInterrupt:
        pass
    finally:
        os._exit(0)


if __name__ == "__main__":
    main()
