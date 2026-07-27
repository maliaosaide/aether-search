"""Brave Search API connector.

Request format verified against the official Brave endpoint as used by
multi-search-mcp/src/providers/brave.ts (GET /res/v1/web/search,
X-Subscription-Token header, data.web.results[]).
"""

import httpx

from .base import Connector, SearchResult, Source
from ..config import settings


class BraveConnector(Connector):
    name = "brave"

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
