"""DeepSeek Search API: structured results for the existing Agent channel."""

import httpx

from .base import Connector, SearchResult, Source
from ..config import settings


class DeepSeekConnector(Connector):
    name = "deepseek"

    def is_configured(self) -> bool:
        return bool(settings.deepseek_api_key)

    async def search(self, query: str, top_k: int = 10) -> SearchResult:
        if not query.strip() or len(query) > 128:
            return SearchResult(sources=[], query=query, connector_name=self.name,
                                error="DeepSeek query must be 1–128 Unicode characters and not blank")
        count = max(1, min(top_k, 50))
        try:
            async with httpx.AsyncClient(timeout=settings.connector_timeout) as client:
                resp = await client.post(
                    f"{settings.deepseek_api_url.rstrip('/')}/v1/tools/search",
                    headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                    json={"query": query, "count": count, "need_content": False},
                )
                resp.raise_for_status()
                items = resp.json().get("results") or []
        except Exception as e:
            return SearchResult(sources=[], query=query, connector_name=self.name, error=str(e))

        sources = [
            Source(
                title=r.get("title") or r.get("url", ""),
                url=r.get("url", ""),
                content=r.get("summary") or r.get("snippet") or "",
                connector=self.name,
                metadata={"published_date": r.get("date"), "rerank_score": r.get("rerank_score"),
                          "authority": r.get("authority")},
            )
            for r in items[:count]
            if r.get("url")
        ]
        return SearchResult(sources=sources, query=query, connector_name=self.name)
