"""Connector contract shared by all aux search backends. Adapted from gigaxity connectors/base.py."""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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


class Connector(ABC):
    name: str = "base"
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

    def is_configured(self) -> bool:
        return True
