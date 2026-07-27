"""Aux channel orchestration: LLM rewrite -> concurrent fan-out -> RRF fusion."""

import asyncio

from .config import settings
from .connectors import Source, active_connectors
from .fusion import rrf_fusion
from .llm import LLMClient

REWRITE_SYSTEM = (
    "You are a search-query planner. Given a user query, output up to {n} short, "
    "genuinely different web-search queries that together cover the intent "
    "(different angles, synonyms, or narrower scopes). Keep the language of the "
    "original query. One query per line, no numbering, no commentary."
)

DIGEST_SYSTEM = (
    "You compress web search results. Given a query and numbered sources, write a "
    "brief factual digest (<=200 words) answering the query, citing sources as [N]. "
    "Only state what the sources support."
)


class AuxAgent:
    def __init__(self):
        self.llm = LLMClient()
        self.connectors = active_connectors()

    async def rewrite(self, query: str) -> list[str]:
        """Return [query, *variants]; degrade to [query] on any failure."""
        if not (settings.llm_rewrite and self.llm.is_configured() and settings.max_variants > 1):
            return [query]
        try:
            raw = await self.llm.complete(
                REWRITE_SYSTEM.format(n=settings.max_variants - 1), query, max_tokens=200
            )
        except Exception:
            return [query]
        seen = {query.strip().lower()}
        variants = [query]
        for line in raw.splitlines():
            q = line.strip().strip("-*0123456789. ")
            if q and q.lower() not in seen:
                seen.add(q.lower())
                variants.append(q)
            if len(variants) >= settings.max_variants:
                break
        return variants

    def _assigned(self, queries: list[str], connector) -> list[str]:
        """Which rewritten queries a connector should run.
        multi_query / full fanout: every variant. economy: the original query,
        with the remaining variants round-robin'd across single-query backends."""
        if connector.multi_query or settings.fanout == "full":
            return queries
        single = [c for c in self.connectors if not c.multi_query]
        if len(queries) == 1 or not single:
            return queries[:1]
        idx = single.index(connector)
        qs = [queries[0]]
        for i, v in enumerate(queries[1:]):
            if i % len(single) == idx:
                qs.append(v)
        return qs

    async def run(self, query: str) -> tuple[list[Source], dict]:
        """Returns (fused sources, stats)."""
        stats: dict = {"connectors": [c.name for c in self.connectors], "variants": [], "errors": []}
        if not self.connectors:
            return [], stats

        queries = await self.rewrite(query)
        stats["variants"] = queries

        sem = asyncio.Semaphore(settings.max_concurrency)

        async def bounded(connector):
            async with sem:
                return await connector.search_all(self._assigned(queries, connector), settings.top_k)

        results = await asyncio.gather(
            *(bounded(c) for c in self.connectors), return_exceptions=True
        )

        lists: list[list[Source]] = []
        for r in results:
            if isinstance(r, Exception):
                stats["errors"].append(str(r))
                continue
            for sr in r:
                if sr.error:
                    stats["errors"].append(f"{sr.connector_name}: {sr.error}")
                elif sr.sources:
                    lists.append(sr.sources)

        fused = rrf_fusion(lists, k=settings.rrf_k, top_k=settings.top_k * 2)
        stats["hits"] = len(fused)
        return fused, stats

    async def digest(self, query: str, sources: list[Source]) -> str:
        if not (settings.llm_digest and self.llm.is_configured() and sources):
            return ""
        numbered = "\n".join(
            f"[{i}] {s.title} — {s.url}\n{s.content[:300]}" for i, s in enumerate(sources[:10], 1)
        )
        try:
            return await self.llm.complete(DIGEST_SYSTEM, f"Query: {query}\n\n{numbered}", max_tokens=400)
        except Exception:
            return ""
