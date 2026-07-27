"""Connectors auto-enabled by configured keys."""

from .base import Connector, SearchResult, Source
from .brave import BraveConnector
from .parallel import ParallelConnector
from .exa import ExaConnector
from .firecrawl import FirecrawlConnector
from .linkup import LinkupConnector
from .serper import SerperConnector
from .sonar import SonarConnector
from .tavily import TavilyConnector
from .youcom import YoucomConnector

__all__ = ["Connector", "SearchResult", "Source", "active_connectors"]

_ALL = [
    ParallelConnector, BraveConnector, ExaConnector, SerperConnector, LinkupConnector,
    YoucomConnector, SonarConnector, TavilyConnector, FirecrawlConnector,
]


def active_connectors() -> list[Connector]:
    return [c for cls in _ALL if (c := cls()).is_configured()]
