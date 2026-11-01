"""OpenAI-compatible client for the aux/agent LLM."""

from dataclasses import dataclass

import httpx
from openai import AsyncOpenAI, BadRequestError

from .config import settings


class ToolsNotSupported(RuntimeError):
    """Endpoint rejects the tools parameter (function calling unsupported)."""


@dataclass
class ToolCallResult:
    content: str
    tool_calls: list
    raw: dict  # assistant message dict, appendable back into messages


class LLMClient:
    def __init__(self):
        self.model = settings.llm_model
        self._client = AsyncOpenAI(
            base_url=settings.llm_api_base or None,
            api_key=settings.llm_api_key or "not-configured",
            timeout=httpx.Timeout(settings.llm_timeout, connect=10.0),
        )

    def is_configured(self) -> bool:
        return bool(settings.llm_api_key and settings.llm_model)

    async def complete(self, system: str, user: str, max_tokens: int = 512) -> str:
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""

    async def complete_with_tools(
        self, messages: list[dict], tools: list[dict],
        tool_choice: str = "auto", max_tokens: int = 2048,
    ) -> ToolCallResult:
        try:
            resp = await self._client.chat.completions.create(
                model=self.model, messages=messages, tools=tools,
                tool_choice=tool_choice, temperature=0.3, max_tokens=max_tokens,
            )
        except BadRequestError as e:
            low = str(e).lower()
            if any(k in low for k in ("tool", "function", "unsupported")):
                raise ToolsNotSupported(str(e)) from e
            raise
        m = resp.choices[0].message
        raw = {"role": "assistant", "content": m.content}
        if m.tool_calls:
            raw["tool_calls"] = m.tool_calls
        # DeepSeek thinking-mode tool calls require this field on subsequent requests.
        if getattr(m, "reasoning_content", None) is not None:
            raw["reasoning_content"] = m.reasoning_content
        return ToolCallResult(m.content or "", m.tool_calls or [], raw)
