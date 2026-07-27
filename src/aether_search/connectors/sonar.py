"""Perplexity Sonar connector — LLM search whose search_results we harvest as sources.

Request format verified against docs.perplexity.ai/api-reference/chat-completions-post
(fetched 2026-07-26): POST {base}/chat/completions, Bearer auth, OpenAI-compatible
body. Response carries search_results[].{title,url,snippet,date} alongside the answer.
"""

import httpx

from .base import Connector, SearchResult, Source
from ..config import settings


class SonarConnector(Connector):
    name = "sonar"

    def is_configured(self) -> bool:
        return bool(settings.sonar_api_key)

    async def search(self, query: str, top_k: int = 10) -> SearchResult:
        try:
            # Sonar runs an LLM behind the search; allow it more time than raw APIs.
            async with httpx.AsyncClient(timeout=max(settings.connector_timeout, 20.0)) as client:
                resp = await client.post(
                    f"{settings.sonar_api_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.sonar_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": settings.sonar_model,
                        "messages": [{"role": "user", "content": query}],
                        "max_tokens": 1,
                        "web_search_options": {"search_context_size": "low"},
                    },
                )
                resp.raise_for_status()
                items = resp.json().get("search_results") or []
        except Exception as e:
            return SearchResult(sources=[], query=query, connector_name=self.name, error=str(e))

        sources = [
            Source(
                title=r.get("title", ""),
                url=r.get("url", ""),
                content=r.get("snippet", ""),
                connector=self.name,
                metadata={"date": r.get("date")},
            )
            for r in items[:top_k]
            if r.get("url")
        ]
        return SearchResult(sources=sources, query=query, connector_name=self.name)
