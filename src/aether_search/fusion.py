"""Reciprocal Rank Fusion. Ported from gigaxity search/fusion.py."""

from .connectors.base import Source


def rrf_fusion(results_lists: list[list[Source]], k: int = 60, top_k: int = 20) -> list[Source]:
    scores: dict[str, float] = {}
    by_url: dict[str, Source] = {}

    for results in results_lists:
        for rank, source in enumerate(results, start=1):
            key = source.url
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            if key not in by_url:
                by_url[key] = source

    fused = []
    for key in sorted(scores, key=lambda x: scores[x], reverse=True)[:top_k]:
        source = by_url[key]
        source.score = scores[key]
        fused.append(source)
    return fused
