"""Tavily connector. Disabled by default — grok-search already spends this quota.

Request format verified against tavily-mcp/src/index.ts:69,103:
POST https://api.tavily.com/search, Bearer auth. Response:
results[].{title,url,content,score,published_date}.
"""

import httpx

from .base import Connector, ImageResult, ImageSearchResult, SearchResult, Source, http_error
from ..config import settings


class TavilyConnector(Connector):
    name = "tavily"
    supports_images = True

    def is_configured(self) -> bool:
        return bool(settings.tavily_api_key)

    async def search(self, query: str, top_k: int = 10) -> SearchResult:
        try:
            async with httpx.AsyncClient(timeout=settings.connector_timeout) as client:
                resp = await client.post(
                    f"{settings.tavily_api_url.rstrip('/')}/search",
                    headers={
                        "Authorization": f"Bearer {settings.tavily_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"query": query, "max_results": top_k, "search_depth": "basic"},
                )
                resp.raise_for_status()
                items = resp.json().get("results") or []
        except Exception as e:
            return SearchResult(sources=[], query=query, connector_name=self.name, error=str(e))

        sources = [
            Source(
                title=r.get("title", ""),
                url=r.get("url", ""),
                content=r.get("content", ""),
                connector=self.name,
                metadata={"published_date": r.get("published_date")},
            )
            for r in items[:top_k]
            if r.get("url")
        ]
        return SearchResult(sources=sources, query=query, connector_name=self.name)

    async def search_images(self, query: str, top_k: int = 10) -> ImageSearchResult:
        try:
            async with httpx.AsyncClient(timeout=settings.connector_timeout) as client:
                resp = await client.post(
                    f"{settings.tavily_api_url.rstrip('/')}/search",
                    headers={
                        "Authorization": f"Bearer {settings.tavily_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "query": query,
                        "max_results": max(1, min(top_k, 20)),
                        "search_depth": "basic",
                        "include_images": True,
                        "include_image_descriptions": True,
                    },
                )
                resp.raise_for_status()
                items = resp.json().get("images") or []
        except Exception as e:
            return ImageSearchResult(images=[], query=query, connector_name=self.name, error=http_error(e))

        images = []
        for item in items[:top_k]:
            if isinstance(item, str):
                url, description = item, ""
            else:
                url = item.get("url") or ""
                description = item.get("description") or ""
                if not isinstance(url, str):
                    url = ""
                if not isinstance(description, str):
                    description = str(description)
            if not url:
                continue
            images.append(ImageResult(
                title=description[:120],
                image_url=url,
                description=description,
                connector=self.name,
            ))
        return ImageSearchResult(images=images, query=query, connector_name=self.name)
