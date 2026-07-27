"""Exa semantic search connector.

Request format verified against exa.ai/docs/reference/search (fetched 2026-07-26):
POST https://api.exa.ai/search, auth via x-api-key header, content params MUST
nest under "contents" (top-level highlights returns 400). Response:
results[].{title,url,highlights[],publishedDate}.
"""

import httpx

from .base import Connector, SearchResult, Source
from ..config import settings


class ExaConnector(Connector):
    name = "exa"

    def is_configured(self) -> bool:
        return bool(settings.exa_api_key)

    async def search(self, query: str, top_k: int = 10) -> SearchResult:
        try:
            async with httpx.AsyncClient(timeout=settings.connector_timeout) as client:
                resp = await client.post(
                    f"{settings.exa_api_url.rstrip('/')}/search",
                    headers={"x-api-key": settings.exa_api_key},
                    json={
                        "query": query,
                        "type": "fast",
                        "numResults": top_k,
                        "contents": {"highlights": True},
                    },
                )
                resp.raise_for_status()
                items = resp.json().get("results") or []
        except Exception as e:
            return SearchResult(sources=[], query=query, connector_name=self.name, error=str(e))

        sources = [
            Source(
                title=r.get("title") or r.get("url", ""),
                url=r.get("url", ""),
                content=" ".join(r.get("highlights") or []),
                connector=self.name,
                metadata={"published_date": r.get("publishedDate")},
            )
            for r in items[:top_k]
            if r.get("url")
        ]
        return SearchResult(sources=sources, query=query, connector_name=self.name)
