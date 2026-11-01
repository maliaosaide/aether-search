"""Page fetcher: DeepSeek Fetch -> Tavily Extract -> Firecrawl Scrape, plus Tavily Map.

Request formats reference grok-search server.py (verified code), rewritten aether-style.
/search is NOT here — it lives in the Tavily/Firecrawl connectors (agent seed search).
"""

import json

import httpx

from .config import settings


def configured() -> bool:
    return bool(settings.deepseek_api_key or settings.tavily_api_key or settings.firecrawl_api_key)


async def deepseek_fetch(url: str) -> str | None:
    if not settings.deepseek_api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=settings.fetch_timeout) as client:
            resp = await client.post(
                f"{settings.deepseek_api_url.rstrip('/')}/v1/tools/fetch",
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                json={"url": url},
            )
            resp.raise_for_status()
            content = resp.json().get("content", "")
            return content if content and content.strip() else None
    except Exception:
        return None


async def tavily_extract(url: str) -> str | None:
    if not settings.tavily_api_key:
        return None
    endpoint = f"{settings.tavily_api_url.rstrip('/')}/extract"
    headers = {"Authorization": f"Bearer {settings.tavily_api_key}", "Content-Type": "application/json"}
    body = {"urls": [url], "format": "markdown"}
    try:
        async with httpx.AsyncClient(timeout=settings.fetch_timeout) as client:
            resp = await client.post(endpoint, headers=headers, json=body)
            resp.raise_for_status()
            results = resp.json().get("results") or []
            if results:
                content = results[0].get("raw_content", "")
                return content if content and content.strip() else None
            return None
    except Exception:
        return None


async def firecrawl_scrape(url: str) -> str | None:
    if not settings.firecrawl_api_key:
        return None
    endpoint = f"{settings.firecrawl_api_url.rstrip('/')}/scrape"
    headers = {"Authorization": f"Bearer {settings.firecrawl_api_key}", "Content-Type": "application/json"}
    for attempt in range(settings.scrape_retry):
        body = {"url": url, "formats": ["markdown"], "timeout": 60000, "waitFor": (attempt + 1) * 1500}
        try:
            async with httpx.AsyncClient(timeout=settings.fetch_timeout) as client:
                resp = await client.post(endpoint, headers=headers, json=body)
                resp.raise_for_status()
                markdown = (resp.json().get("data") or {}).get("markdown", "")
                if markdown and markdown.strip():
                    return markdown
        except Exception:
            return None
    return None


async def fetch(url: str) -> str:
    """DeepSeek Fetch -> Tavily Extract -> Firecrawl Scrape degradation."""
    result = await deepseek_fetch(url)
    if result:
        return result
    result = await tavily_extract(url)
    if result:
        return result
    result = await firecrawl_scrape(url)
    if result:
        return result
    if not configured():
        return "配置错误: 请配置 AETHER_DEEPSEEK_API_KEY、AETHER_TAVILY_API_KEY 或 AETHER_FIRECRAWL_API_KEY"
    return "提取失败: 所有提取服务均未能获取内容"


async def tavily_map(url: str, instructions: str = "", max_depth: int = 1,
                     max_breadth: int = 20, limit: int = 50, timeout: int = 150) -> str:
    if not settings.tavily_api_key:
        return "配置错误: AETHER_TAVILY_API_KEY 未配置"
    endpoint = f"{settings.tavily_api_url.rstrip('/')}/map"
    headers = {"Authorization": f"Bearer {settings.tavily_api_key}", "Content-Type": "application/json"}
    body: dict = {"url": url, "max_depth": max_depth, "max_breadth": max_breadth, "limit": limit, "timeout": timeout}
    if instructions:
        body["instructions"] = instructions
    try:
        async with httpx.AsyncClient(timeout=float(timeout + 10)) as client:
            resp = await client.post(endpoint, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
            return json.dumps({"base_url": data.get("base_url", ""), "results": data.get("results", []),
                               "response_time": data.get("response_time", 0)}, ensure_ascii=False, indent=2)
    except httpx.TimeoutException:
        return f"映射超时: 请求超过{timeout}秒"
    except httpx.HTTPStatusError as e:
        return f"HTTP错误: {e.response.status_code} - {e.response.text[:200]}"
    except Exception as e:
        return f"映射错误: {str(e)}"
