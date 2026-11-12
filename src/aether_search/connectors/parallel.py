"""Parallel Search API connector.

Verified against docs.parallel.ai/api-reference/search/search (llms-full.txt):
POST https://api.parallel.ai/v1/search, x-api-key header. Body requires `objective`
(natural-language goal) and `search_queries` (>=1 keyword query); response
results[].{url,title,publish_date,excerpts[]} ordered by decreasing relevance.

Parallel's sweet spot is one call covering multiple angles: a natural-language
objective plus 2-3 diverse keyword queries in a single request. So we override
search_all to fold every AuxAgent variant into one call — objective = the
original query, search_queries = all variants.
"""

import httpx

from .base import Connector, ImageResult, ImageSearchResult, SearchResult, Source, http_error
from ..config import settings


class ParallelConnector(Connector):
    name = "parallel"
    multi_query = True
    supports_images = True

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
                        "x-api-key": settings.parallel_api_key,
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

    async def search_images(self, query: str, top_k: int = 10) -> ImageSearchResult:
        try:
            async with httpx.AsyncClient(timeout=settings.connector_timeout) as client:
                resp = await client.post(
                    f"{settings.parallel_api_url.rstrip('/')}/images/search",
                    headers={
                        "x-api-key": settings.parallel_api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "objective": query,
                        "search_queries": [query[:200]],
                        "mode": "advanced",
                        "advanced_settings": {"max_results": top_k},
                    },
                )
                resp.raise_for_status()
                items = resp.json().get("results") or []
        except Exception as e:
            return ImageSearchResult(images=[], query=query, connector_name=self.name, error=http_error(e))

        images = [
            ImageResult(
                title=r.get("title", ""),
                image_url=r.get("image_url", ""),
                source_url=r.get("source_page_url", ""),
                width=r.get("width") or 0,
                height=r.get("height") or 0,
                connector=self.name,
            )
            for r in items[:top_k]
            if r.get("image_url")
        ]
        return ImageSearchResult(images=images, query=query, connector_name=self.name)
