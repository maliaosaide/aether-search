"""DeepSeek Search/Fetch 与 Agent 的离线测试；不读取 .env，不访问外部服务。"""

import json
import os
import re
import sys
import unittest
from contextlib import chdir
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from fastmcp import Client
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
clean_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("AETHER_")}
with TemporaryDirectory(dir=ROOT) as empty_dir, chdir(empty_dir), patch.dict(os.environ, clean_env, clear=True):
    from aether_search import config, fetcher, llm, server
    from aether_search.connectors import active_connectors
    from aether_search.connectors.deepseek import DeepSeekConnector


def tool_text(result):
    return "\n".join(item.text for item in result.content if hasattr(item, "text"))


def search_response(**overrides):
    item = {"title": "Documentation", "url": "https://docs.example/page", "summary": "Long summary",
            "snippet": "Short snippet", "content": "", "rerank_score": 2.4, "authority": 3,
            "date": "2026-11-01T00:00:00Z"}
    item.update(overrides)
    return httpx.Response(200, json={"results": [item]})


def completion(content, tool_calls=None, reasoning_content=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return httpx.Response(200, json={
        "id": "test", "object": "chat.completion", "created": 1, "model": "test",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if tool_calls else "stop"}],
    })


class TestDeepSeek(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, clean_env, clear=True))
        defaults = config.Settings(_env_file=None)
        for name in config.Settings.model_fields:
            self.enterContext(patch.object(config.settings, name, getattr(defaults, name)))
        config.settings.deepseek_api_key = "synthetic-deepseek-key"
        config.settings.verify = False
        self.requests = []
        self.responses = []

        def handler(request):
            self.requests.append(request)
            if not self.responses:
                raise AssertionError(f"Unexpected HTTP request: {request.url}")
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        transport = httpx.MockTransport(handler)

        async def send(_transport, request):
            return await transport.handle_async_request(request)

        self.enterContext(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", send))

        # Inject the transport explicitly; newer SDKs default to httpx2 instead of httpx.
        def sdk_client(**kwargs):
            return AsyncOpenAI(**kwargs, max_retries=0,
                               http_client=httpx.AsyncClient(transport=transport, trust_env=False))

        self.enterContext(patch.object(llm, "AsyncOpenAI", side_effect=sdk_client))

    def test_key_enables_search_and_fetch(self):
        self.assertEqual([c.name for c in active_connectors()], ["deepseek"])
        self.assertTrue(fetcher.configured())
        config.settings.deepseek_api_key = ""
        self.assertEqual(active_connectors(), [])
        self.assertFalse(fetcher.configured())

    def test_environment_configuration(self):
        with patch.dict(os.environ, {"AETHER_DEEPSEEK_API_KEY": "env-key",
                                    "AETHER_DEEPSEEK_API_URL": "https://proxy.example"}):
            settings = config.Settings(_env_file=None)
        self.assertEqual(settings.deepseek_api_key, "env-key")
        self.assertEqual(settings.deepseek_api_url, "https://proxy.example")

    async def test_search_request_and_sources(self):
        self.responses = [search_response()]
        result = await DeepSeekConnector().search("检索 site:example.com", top_k=5)
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://api.deepseek.com/v1/tools/search")
        self.assertEqual(request.headers["Authorization"], "Bearer synthetic-deepseek-key")
        self.assertEqual(json.loads(request.content),
                         {"query": "检索 site:example.com", "count": 5, "need_content": False})
        self.assertIsNone(result.error)
        self.assertEqual(result.connector_name, "deepseek")
        source = result.sources[0]
        self.assertEqual((source.title, source.url, source.content, source.connector),
                         ("Documentation", "https://docs.example/page", "Long summary", "deepseek"))
        self.assertEqual(source.metadata, {"published_date": "2026-11-01T00:00:00Z",
                                           "rerank_score": 2.4, "authority": 3})

    async def test_snippet_fallback_and_missing_url(self):
        self.responses = [httpx.Response(200, json={"results": [
            {"url": "https://docs.example/page", "snippet": "Snippet"},
            {"title": "No URL"},
        ]})]
        result = await DeepSeekConnector().search("test")
        self.assertEqual(len(result.sources), 1)
        self.assertEqual(result.sources[0].content, "Snippet")

    async def test_query_limits_count_and_custom_url(self):
        connector = DeepSeekConnector()
        for query in ("", " \t", "中" * 129):
            with self.subTest(query=query):
                result = await connector.search(query)
                self.assertIsNotNone(result.error)
                self.assertEqual(result.sources, [])
        self.assertEqual(self.requests, [])
        config.settings.deepseek_api_url = "https://proxy.example/"
        for top_k, expected in ((0, 1), (100, 50)):
            with self.subTest(top_k=top_k):
                self.responses.append(search_response())
                result = await connector.search("中" * 128, top_k=top_k)
                self.assertIsNone(result.error)
                self.assertEqual(json.loads(self.requests[-1].content)["count"], expected)
                self.assertEqual(str(self.requests[-1].url), "https://proxy.example/v1/tools/search")

    async def test_empty_results_are_not_errors(self):
        self.responses = [httpx.Response(200, json={"results": []})]
        result = await DeepSeekConnector().search("test")
        self.assertEqual(result.sources, [])
        self.assertIsNone(result.error)

    async def test_http_errors_and_timeout_return_search_errors(self):
        responses = [httpx.Response(status, json={"error": {"message": "test failure"}})
                     for status in (401, 402, 429, 500)] + [httpx.ReadTimeout("test timeout")]
        for response in responses:
            with self.subTest(response=response):
                self.responses = [response]
                before = len(self.requests)
                result = await DeepSeekConnector().search("test")
                self.assertEqual(result.sources, [])
                self.assertIsNotNone(result.error)
                self.assertEqual(len(self.requests), before + 1)

    async def test_fetch_with_only_deepseek(self):
        self.responses = [httpx.Response(200, json={"title": "Page", "content": "# Page\n\nBody"})]
        async with Client(server.mcp) as client:
            result = await client.call_tool("web_fetch", {"url": "https://docs.example/page"})
        self.assertEqual(tool_text(result), "# Page\n\nBody")
        self.assertEqual(str(self.requests[0].url), "https://api.deepseek.com/v1/tools/fetch")
        self.assertEqual(json.loads(self.requests[0].content), {"url": "https://docs.example/page"})
        self.assertEqual(self.requests[0].headers["Authorization"], "Bearer synthetic-deepseek-key")

    async def test_fetch_falls_back_to_existing_services(self):
        config.settings.tavily_api_key = "synthetic-tavily-key"
        config.settings.firecrawl_api_key = "synthetic-firecrawl-key"
        self.responses = [httpx.Response(404), httpx.Response(200, json={"results": []}),
                          httpx.Response(200, json={"data": {"markdown": "Fallback page"}})]
        self.assertEqual(await fetcher.fetch("https://docs.example/page"), "Fallback page")
        self.assertEqual([r.url.path for r in self.requests],
                         ["/v1/tools/fetch", "/extract", "/v2/scrape"])

    async def test_empty_fetch_and_unconfigured_fetch(self):
        self.responses = [httpx.Response(200, json={"content": " \n"})]
        self.assertIn("提取失败", await fetcher.fetch("https://docs.example/page"))
        config.settings.deepseek_api_key = ""
        self.assertIn("配置错误", await fetcher.fetch("https://docs.example/page"))
        self.assertEqual(len(self.requests), 1)

    async def test_agent_only_search_and_source_cache(self):
        self.responses = [search_response()]
        async with Client(server.mcp) as client:
            output = tool_text(await client.call_tool("search", {"query": "test"}))
            session = re.search(r"session_id: ([a-f0-9]+)", output).group(1)
            sources = json.loads(tool_text(await client.call_tool("get_sources", {"session_id": session})))
            status = tool_text(await client.call_tool("get_config", {}))
        self.assertIn("Long summary", output)
        self.assertNotIn("grok:", output)
        self.assertEqual(sources["sources_count"], 1)
        self.assertEqual(sources["sources"][0]["connector"], "deepseek")
        self.assertIn("aux backends: deepseek", status)
        self.assertEqual(len(self.requests), 1)

    async def test_agent_fetch_loop_preserves_reasoning_and_long_summary(self):
        config.settings.llm_api_base = "https://deepseek.test/v1"
        config.settings.llm_api_key = "synthetic-llm-key"
        config.settings.llm_model = "deepseek-flash"
        call = {"id": "call_fetch", "type": "function", "function": {
            "name": "fetch_page", "arguments": json.dumps({"url": "https://docs.example/page"})}}
        summary = "证据" * 200 + "摘要末尾"
        self.responses = [search_response(summary=summary),
                          completion(None, [call], "synthetic continuation"),
                          httpx.Response(200, json={"content": "Fetched full page"}),
                          completion("Answer [1].\n\nSources:\n[1] https://docs.example/page")]
        async with Client(server.mcp) as client:
            output = tool_text(await client.call_tool("search", {"query": "test"}))
        self.assertEqual([r.url.path for r in self.requests],
                         ["/v1/tools/search", "/v1/chat/completions", "/v1/tools/fetch", "/v1/chat/completions"])
        self.assertIn("Answer [1]", output)
        initial = json.loads(self.requests[1].content)
        self.assertIn(summary, initial["messages"][1]["content"])
        self.assertIn("fetch_page", [t["function"]["name"] for t in initial["tools"]])
        messages = json.loads(self.requests[3].content)["messages"]
        self.assertEqual(messages[-2]["reasoning_content"], "synthetic continuation")
        self.assertEqual(messages[-1]["content"], "Fetched full page")
        self.assertEqual(messages[-1]["tool_call_id"], "call_fetch")


if __name__ == "__main__":
    unittest.main()
