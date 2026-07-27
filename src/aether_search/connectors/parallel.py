"""Parallel Search API connector.

Verified against docs.parallel.ai/api-reference/search/search (llms-full.txt):
POST https://api.parallel.ai/v1/search, Bearer auth. Body requires `objective`
(natural-language goal) and `search_queries` (>=1 keyword query); response
results[].{url,title,publish_date,excerpts[]} ordered by decreasing relevance.

Parallel's sweet spot is one call covering multiple angles: a natural-language
objective plus 2-3 diverse keyword queries in a single request. So we override
search_all to fold every AuxAgent variant into one call — objective = the
original query, search_queries = all variants.
"""

import httpx

from .base import Connector, SearchResult, Source
from ..config import settings


class ParallelConnector(Connector):
    name = "parallel"
    multi_query = True

    def is_configured(self) -> bool:
        return bool(settings.parallel_api_key)

    async def search(self, query: str, top_k: int = 10) -> SearchResult:
        return await self._request([query], query, top_k)

    async def search_all(self, queries: list[str], top_k: int = 10) -> list[SearchResult]:
        if not queries:
            return []
        return [await self._request(queries, queries[0], top_k)]

    async def _request(self, search_queries: list[str], objective: str, top_k: int) -> SearchResult:
        try:
            async with httpx.AsyncClient(timeout=settings.connector_timeout) as client:
                resp = await client.post(
                    f"{settings.parallel_api_url.rstrip('/')}/search",
                    headers={
                        "Authorization": f"Bearer {settings.parallel_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "objective": objective,
                        "search_queries": search_queries,
                        "mode": "advanced",
                        "advanced_settings": {"max_results": top_k},
                    },
                )
                resp.raise_for_status()
                items = resp.json().get("results") or []
        except Exception as e:
            return SearchResult(sources=[], query=objective, connector_name=self.name, error=str(e))

        sources = []
        for r in items[:top_k]:
            excerpts = r.get("excerpts") or []
            content = "\n".join(excerpts) if isinstance(excerpts, list) else str(excerpts)
            sources.append(Source(
                title=r.get("title", ""),
                url=r.get("url", ""),
                content=content,
                connector=self.name,
                metadata={"publish_date": r.get("publish_date")},
            ))
        return SearchResult(sources=sources, query=objective, connector_name=self.name)
