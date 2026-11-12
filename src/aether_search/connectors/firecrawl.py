"""Firecrawl search connector. Disabled by default — grok-search may share this quota.

Request format verified against grok-search server.py:_call_firecrawl_search
(production code): POST {base}/search, Bearer auth, body {query, limit}.
Response: data.web[].{title,url,description}.
"""

import httpx

from .base import Connector, ImageResult, ImageSearchResult, SearchResult, Source, http_error
from ..config import settings


class FirecrawlConnector(Connector):
    name = "firecrawl"
    supports_images = True

    def is_configured(self) -> bool:
        return bool(settings.firecrawl_api_key)

    async def search(self, query: str, top_k: int = 10) -> SearchResult:
        try:
            async with httpx.AsyncClient(timeout=settings.connector_timeout) as client:
                resp = await client.post(
                    f"{settings.firecrawl_api_url.rstrip('/')}/search",
                    headers={
                        "Authorization": f"Bearer {settings.firecrawl_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"query": query, "limit": top_k},
                )
                resp.raise_for_status()
                items = (resp.json().get("data") or {}).get("web") or []
        except Exception as e:
            return SearchResult(sources=[], query=query, connector_name=self.name, error=str(e))

        sources = [
            Source(
                title=r.get("title", ""),
                url=r.get("url", ""),
                content=r.get("description", ""),
                connector=self.name,
            )
            for r in items[:top_k]
            if r.get("url")
        ]
        return SearchResult(sources=sources, query=query, connector_name=self.name)

    async def search_images(self, query: str, top_k: int = 10) -> ImageSearchResult:
        try:
            async with httpx.AsyncClient(timeout=settings.connector_timeout) as client:
                resp = await client.post(
                    f"{settings.firecrawl_api_url.rstrip('/')}/search",
                    headers={
                        "Authorization": f"Bearer {settings.firecrawl_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"query": query, "sources": ["images"], "limit": top_k},
                )
                resp.raise_for_status()
                items = (resp.json().get("data") or {}).get("images") or []
        except Exception as e:
            return ImageSearchResult(images=[], query=query, connector_name=self.name, error=http_error(e))

        images = [
            ImageResult(
                title=r.get("title", ""),
                image_url=r.get("imageUrl", ""),
                source_url=r.get("url", ""),
                width=r.get("imageWidth") or 0,
                height=r.get("imageHeight") or 0,
                connector=self.name,
            )
            for r in items[:top_k]
            if r.get("imageUrl")
        ]
        return ImageSearchResult(images=images, query=query, connector_name=self.name)
