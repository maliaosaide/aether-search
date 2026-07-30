"""Aux retrieval: LLM rewrite -> concurrent fan-out -> RRF fusion.

Backends split into primary (default fan-out) and fallback (search_deep only) via
AETHER_FALLBACK_BACKENDS. run_queries/run_fallback share _fanout; run = rewrite + run_queries.
"""

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
        fb = {n.strip() for n in settings.fallback_backends.split(",") if n.strip()}
        self.primary_connectors = [c for c in self.connectors if c.name not in fb]
        self.fallback_connectors = [c for c in self.connectors if c.name in fb]

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

    def _assigned(self, queries: list[str], connector, group: list) -> list[str]:
        """Which rewritten queries a connector should run, within the given group."""
        if connector.multi_query or settings.fanout == "full":
            return queries
        single = [c for c in group if not c.multi_query]
        if len(queries) == 1 or not single:
            return queries[:1]
        idx = single.index(connector)
        qs = [queries[0]]
        for i, v in enumerate(queries[1:]):
            if i % len(single) == idx:
                qs.append(v)
        return qs

    async def _fanout(self, queries: list[str], connectors: list) -> tuple[list[Source], dict]:
        """Concurrent fan-out + RRF over a given connector group. Returns (fused, stats)."""
        stats: dict = {"connectors": [c.name for c in connectors], "variants": queries, "errors": []}
        if not connectors:
            return [], stats

        sem = asyncio.Semaphore(settings.max_concurrency)

        async def bounded(connector):
            async with sem:
                return await connector.search_all(self._assigned(queries, connector, connectors), settings.top_k)

        results = await asyncio.gather(*(bounded(c) for c in connectors), return_exceptions=True)

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

    async def run_queries(self, queries: list[str]) -> tuple[list[Source], dict]:
        """Primary-layer fan-out + RRF, skipping rewrite."""
        return await self._fanout(queries, self.primary_connectors)

    async def run_fallback(self, queries: list[str]) -> tuple[list[Source], dict]:
        """Fallback-layer fan-out + RRF (for search_deep)."""
        return await self._fanout(queries, self.fallback_connectors)

    async def run(self, query: str) -> tuple[list[Source], dict]:
        """Returns (fused sources, stats). Primary layer with rewrite."""
        queries = await self.rewrite(query)
        return await self.run_queries(queries)
