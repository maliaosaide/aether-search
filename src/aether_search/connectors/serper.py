"""Serper (Google SERP) connector.

Request format verified against mcp-server-serper/src/services/serper-client.ts:
POST https://google.serper.dev/search, X-API-KEY header, JSON body {q, num}.
Response: organic[].{title,link,snippet,date}.
"""

import httpx

from .base import Connector, SearchResult, Source
from ..config import settings


class SerperConnector(Connector):
    name = "serper"

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
