"""Channel-shared result contract for aether-search.

本模块是 ``AnswerResult`` 的唯一权威定义:Grok 通道与 Agent 通道都机械地
导入同一个类,不允许各自保留第二份重复定义。

- Grok evidence 恒为 ``[]``(纯 LLM 通道,无结构化检索证据)。
- 失败时 ``text`` 必须为空字符串,以便外层并行 fallback 继续工作。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 避免运行时依赖 connectors 包
    from aether_search.connectors.base import Source


@dataclass
class AnswerResult:
    """Single-channel answer outcome.

    字段顺序与"全部必填"属于公共契约(消费方可能按位置构造),不得改动:
    位置构造为 ``AnswerResult(channel, text, citations, evidence, detail,
    elapsed, error)``。
    """

    channel: str
    text: str
    citations: list[str]
    evidence: list["Source"]
    detail: str
    elapsed: float
    error: str | None


__all__ = ["AnswerResult"]
