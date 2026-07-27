"""Grok primary channel — LLM-native web search over an OpenAI-compatible endpoint.

Ported from grok-search src/grok_search/providers/grok.py (SSE streaming,
retry with Retry-After, time-context injection). Trimmed to the search path.
"""

import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential
from tenacity.wait import wait_base

from .config import settings

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

SEARCH_PROMPT = """\
You are a web research agent. Search the web thoroughly before answering.

1. Infer the user's true intent; brainstorm multiple angles and run parallel searches.
2. Verify every claim; prioritize authoritative sources (official docs, academic, reputable media).
3. Search in English first for volume/quality; switch to Chinese when the topic demands.
4. Every claim must carry its source URL inline. No citation, no claim.
5. Output polished Markdown, most probable answer first. End with a "Sources:" list of all cited URLs, one per line.
"""


def _time_context() -> str:
    now = datetime.now().astimezone()
    return f"[Current time: {now.strftime('%Y-%m-%d %H:%M %Z')}]\n"


def _is_retryable(exc) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in RETRYABLE_STATUS


class _WaitRetryAfter(wait_base):
    def __init__(self):
        self._base = wait_random_exponential(multiplier=1, max=10)

    def __call__(self, retry_state):
        if retry_state.outcome and retry_state.outcome.failed:
            exc = retry_state.outcome.exception()
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                header = (exc.response.headers.get("Retry-After") or "").strip()
                if header.isdigit():
                    return float(header)
                try:
                    dt = parsedate_to_datetime(header)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError):
                    pass
        return self._base(retry_state)


class GrokProvider:
    def is_configured(self) -> bool:
        return bool(settings.grok_api_url and settings.grok_api_key)

    def _effective_model(self) -> str:
        # OpenRouter enables web search via the ":online" model suffix.
        model = settings.grok_model
        if "openrouter" in settings.grok_api_url and ":online" not in model:
            return f"{model}:online"
        return model

    async def search(self, query: str, platform: str = "") -> str:
        """Return grok's cited markdown answer."""
        user = _time_context() + query
        if platform:
            user += f"\n\nFocus your web search on these platforms: {platform}"
        payload = {
            "model": self._effective_model(),
            "messages": [
                {"role": "system", "content": SEARCH_PROMPT},
                {"role": "user", "content": user},
            ],
            "stream": True,
        }
        # xAI Live Search (docs.x.ai): search_parameters in the request body.
        # Unset -> enabled only on api.x.ai; other gateways may reject the field.
        mode = settings.grok_search_mode
        if not mode:
            mode = "auto" if "x.ai" in settings.grok_api_url else "none"
        if mode in ("auto", "on", "off"):
            payload["search_parameters"] = {"mode": mode, "return_citations": True}
        headers = {
            "Authorization": f"Bearer {settings.grok_api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(connect=6.0, read=settings.grok_timeout, write=10.0, pool=None)

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(settings.retry_max_attempts + 1),
                wait=_WaitRetryAfter(),
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    async with client.stream(
                        "POST",
                        f"{settings.grok_api_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as response:
                        response.raise_for_status()
                        return await self._parse_stream(response)
        return ""

    async def _parse_stream(self, response) -> str:
        content = ""
        buffer = []
        async for line in response.aiter_lines():
            line = line.strip()
            if not line:
                continue
            buffer.append(line)
            if line.startswith("data:"):
                if line in ("data: [DONE]", "data:[DONE]"):
                    continue
                try:
                    data = json.loads(line[5:].lstrip())
                    choices = data.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        if "content" in delta:
                            content += delta["content"]
                except (json.JSONDecodeError, IndexError):
                    continue
        # Non-streaming JSON fallback (some gateways ignore stream=true)
        if not content and buffer:
            try:
                data = json.loads("".join(buffer))
                if data.get("choices"):
                    content = data["choices"][0].get("message", {}).get("content", "")
            except json.JSONDecodeError:
                pass
        return content
