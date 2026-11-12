"""Linkup connector.

Request format verified against docs.linkup.so/pages/documentation/endpoints/search/reference
(fetched 2026-07-26): POST https://api.linkup.so/v1/search, Bearer auth,
body {q, depth, outputType:"searchResults", maxResults}. Response:
results[].{name,url,content}. depth "standard" = $0.005/call tier.
"""

import httpx

from .base import Connector, ImageResult, ImageSearchResult, SearchResult, Source, http_error
from ..config import settings


class LinkupConnector(Connector):
    name = "linkup"
    supports_images = True

    def is_configured(self) -> bool:
        return bool(settings.linkup_api_key)

    async def search(self, query: str, top_k: int = 10) -> SearchResult:
        try:
            async with httpx.AsyncClient(timeout=settings.connector_timeout) as client:
                resp = await client.post(
                    f"{settings.linkup_api_url.rstrip('/')}/search",
                    headers={
                        "Authorization": f"Bearer {settings.linkup_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "q": query,
                        "depth": "standard",
                        "outputType": "searchResults",
                        "maxResults": top_k,
                    },
                )
                resp.raise_for_status()
                items = resp.json().get("results") or []
        except Exception as e:
            return SearchResult(sources=[], query=query, connector_name=self.name, error=str(e))

        sources = [
            Source(
                title=r.get("name") or r.get("url", ""),
                url=r.get("url", ""),
                content=r.get("content", ""),
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
                    f"{settings.linkup_api_url.rstrip('/')}/search",
                    headers={
                        "Authorization": f"Bearer {settings.linkup_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "q": query,
                        "depth": "standard",
                        "outputType": "searchResults",
                        "maxResults": max(top_k * 2, 20),
                        "includeImages": True,
                    },
                )
                resp.raise_for_status()
                items = resp.json().get("results") or []
        except Exception as e:
            return ImageSearchResult(images=[], query=query, connector_name=self.name, error=http_error(e))

        images = [
            ImageResult(
                title=r.get("name") or r.get("url", ""),
                image_url=r.get("url", ""),
                connector=self.name,
            )
            for r in items
            if r.get("type") == "image" and isinstance(r.get("url"), str) and r.get("url")
        ][:top_k]
        return ImageSearchResult(images=images, query=query, connector_name=self.name)
