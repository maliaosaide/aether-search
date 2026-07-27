"""Linkup connector.

Request format verified against docs.linkup.so/pages/documentation/endpoints/search/reference
(fetched 2026-07-26): POST https://api.linkup.so/v1/search, Bearer auth,
body {q, depth, outputType:"searchResults", maxResults}. Response:
results[].{name,url,content}. depth "standard" = $0.005/call tier.
"""

import httpx

from .base import Connector, SearchResult, Source
from ..config import settings


class LinkupConnector(Connector):
    name = "linkup"

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
