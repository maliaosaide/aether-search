"""Connector contract shared by all aux search backends. Adapted from gigaxity connectors/base.py."""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx


def http_error(error: Exception) -> str:
    """Include the upstream response body so quota/auth errors are actionable."""
    if isinstance(error, httpx.HTTPStatusError):
        detail = error.response.text.strip().replace("\n", " ")
        if detail:
            return f"{error} | {detail[:300]}"
    return str(error)


@dataclass
class Source:
    title: str
    url: str
    content: str
    score: float = 0.0
    connector: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class SearchResult:
    sources: list[Source]
    query: str
    connector_name: str
    error: str | None = None


@dataclass
class ImageResult:
    title: str
    image_url: str
    source_url: str = ""
    thumbnail_url: str = ""
    width: int = 0
    height: int = 0
    description: str = ""
    connector: str = ""


@dataclass
class ImageSearchResult:
    images: list[ImageResult]
    query: str
    connector_name: str
    error: str | None = None


class Connector(ABC):
    name: str = "base"
    supports_images: bool = False
    # True = one request ingests every rewritten variant. Parallel is the
    # canonical case: it takes a natural-language objective plus 2-3 diverse
    # keyword queries in a single call, so AuxAgent folds all variants in.
    multi_query: bool = False

    @abstractmethod
    async def search(self, query: str, top_k: int = 10) -> SearchResult:
        ...

    async def search_all(self, queries: list[str], top_k: int = 10) -> list[SearchResult]:
        """Process the rewritten query variants. Default: one search() per query.
        Override to fold all variants into a single multi-query request."""
        return await asyncio.gather(*(self.search(q, top_k) for q in queries))

    async def search_images(self, query: str, top_k: int = 10) -> ImageSearchResult:
        """Image search. Only connectors with supports_images=True are asked."""
        raise NotImplementedError(f"{self.name} declares image support but does not implement search_images")

    def is_configured(self) -> bool:
        return True
