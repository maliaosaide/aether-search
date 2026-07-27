"""OpenAI-compatible client for the aux query agent."""

import httpx
from openai import AsyncOpenAI

from .config import settings


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
