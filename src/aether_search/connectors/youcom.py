"""You.com Web Search connector.

Endpoint https://api.you.com/v1 (updated 2026-07-30 from api.ydc-index.io).
GET {base}/search with X-API-Key header, params {query, count}.
Response: results.web[].{title,url,description,snippets[]}.
"""

import httpx

from .base import Connector, SearchResult, Source
from ..config import settings


class YoucomConnector(Connector):
    name = "youcom"

    def is_configured(self) -> bool:
        return bool(settings.youcom_api_key)

    async def search(self, query: str, top_k: int = 10) -> SearchResult:
        try:
            async with httpx.AsyncClient(timeout=settings.connector_timeout) as client:
                resp = await client.get(
                    f"{settings.youcom_api_url.rstrip('/')}/search",
                    params={"query": query, "count": top_k},
                    headers={"X-API-Key": settings.youcom_api_key},
                )
                resp.raise_for_status()
                items = ((resp.json().get("results") or {}).get("web")) or []
        except Exception as e:
            return SearchResult(sources=[], query=query, connector_name=self.name, error=str(e))

        sources = [
            Source(
                title=r.get("title", ""),
                url=r.get("url", ""),
                content=r.get("description") or " ".join(r.get("snippets") or [])[:500],
                connector=self.name,
            )
            for r in items[:top_k]
            if r.get("url")
        ]
        return SearchResult(sources=sources, query=query, connector_name=self.name)
