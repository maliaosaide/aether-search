"""Brave Search API connector.

Request format verified against the official Brave endpoint as used by
multi-search-mcp/src/providers/brave.ts (GET /res/v1/web/search,
X-Subscription-Token header, data.web.results[]).
"""

import httpx

from .base import Connector, ImageResult, ImageSearchResult, SearchResult, Source, http_error
from ..config import settings


class BraveConnector(Connector):
    name = "brave"
    supports_images = True

    def is_configured(self) -> bool:
        return bool(settings.brave_api_key)

    async def search(self, query: str, top_k: int = 10) -> SearchResult:
        try:
            async with httpx.AsyncClient(timeout=settings.connector_timeout) as client:
                resp = await client.get(
                    f"{settings.brave_api_url.rstrip('/')}/web/search",
                    params={"q": query, "count": min(top_k, 20)},
                    headers={
                        "X-Subscription-Token": settings.brave_api_key,
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip",
                    },
                )
                resp.raise_for_status()
                items = (resp.json().get("web") or {}).get("results") or []
        except Exception as e:
            return SearchResult(sources=[], query=query, connector_name=self.name, error=str(e))

        sources = [
            Source(
                title=r.get("title", ""),
                url=r.get("url", ""),
                content=r.get("description", ""),
                connector=self.name,
                metadata={"age": r.get("age")},
            )
            for r in items[:top_k]
            if r.get("url")
        ]
        return SearchResult(sources=sources, query=query, connector_name=self.name)

    async def search_images(self, query: str, top_k: int = 10) -> ImageSearchResult:
        try:
            async with httpx.AsyncClient(timeout=settings.connector_timeout) as client:
                resp = await client.get(
                    f"{settings.brave_api_url.rstrip('/')}/images/search",
                    params={"q": query, "count": min(top_k, 100)},
                    headers={
                        "X-Subscription-Token": settings.brave_api_key,
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip",
                    },
                )
                resp.raise_for_status()
                items = resp.json().get("results") or []
        except Exception as e:
            return ImageSearchResult(images=[], query=query, connector_name=self.name, error=http_error(e))

        images = []
        for r in items[:top_k]:
            props = r.get("properties") or {}
            thumb = r.get("thumbnail") or {}
            image_url = props.get("url") or thumb.get("src")
            if not image_url:
                continue
            images.append(ImageResult(
                title=r.get("title", ""),
                image_url=image_url,
                source_url=r.get("url", ""),
                thumbnail_url=thumb.get("src", ""),
                width=props.get("width") or thumb.get("width") or 0,
                height=props.get("height") or thumb.get("height") or 0,
                connector=self.name,
            ))
        return ImageSearchResult(images=images, query=query, connector_name=self.name)
