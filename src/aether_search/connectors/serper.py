"""Serper (Google SERP) connector.

Request format verified against mcp-server-serper/src/services/serper-client.ts:
POST https://google.serper.dev/search, X-API-KEY header, JSON body {q, num}.
Response: organic[].{title,link,snippet,date}.
"""

import httpx

from .base import Connector, ImageResult, ImageSearchResult, SearchResult, Source, http_error
from ..config import settings


class SerperConnector(Connector):
    name = "serper"
    supports_images = True

    def is_configured(self) -> bool:
        return bool(settings.serper_api_key)

    async def search(self, query: str, top_k: int = 10) -> SearchResult:
        try:
            async with httpx.AsyncClient(timeout=settings.connector_timeout) as client:
                resp = await client.post(
                    f"{settings.serper_api_url.rstrip('/')}/search",
                    headers={
                        "X-API-KEY": settings.serper_api_key,
                        "Content-Type": "application/json",
                    },
                    json={"q": query, "num": top_k},
                )
                resp.raise_for_status()
                items = resp.json().get("organic") or []
        except Exception as e:
            return SearchResult(sources=[], query=query, connector_name=self.name, error=str(e))

        sources = [
            Source(
                title=r.get("title", ""),
                url=r.get("link", ""),
                content=r.get("snippet", ""),
                connector=self.name,
                metadata={"date": r.get("date")},
            )
            for r in items[:top_k]
            if r.get("link")
        ]
        return SearchResult(sources=sources, query=query, connector_name=self.name)

    async def search_images(self, query: str, top_k: int = 10) -> ImageSearchResult:
        try:
            async with httpx.AsyncClient(timeout=settings.connector_timeout) as client:
                resp = await client.post(
                    f"{settings.serper_api_url.rstrip('/')}/images",
                    headers={
                        "X-API-KEY": settings.serper_api_key,
                        "Content-Type": "application/json",
                    },
                    json={"q": query, "num": top_k},
                )
                resp.raise_for_status()
                items = resp.json().get("images") or []
        except Exception as e:
            return ImageSearchResult(images=[], query=query, connector_name=self.name, error=http_error(e))

        images = [
            ImageResult(
                title=r.get("title", ""),
                image_url=r.get("imageUrl", ""),
                source_url=r.get("link", ""),
                thumbnail_url=r.get("thumbnailUrl", ""),
                width=r.get("imageWidth") or 0,
                height=r.get("imageHeight") or 0,
                connector=self.name,
            )
            for r in items[:top_k]
            if r.get("imageUrl")
        ]
        return ImageSearchResult(images=images, query=query, connector_name=self.name)
