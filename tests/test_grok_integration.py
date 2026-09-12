"""通过进程内 MCP 客户端验证真实编排；只模拟外部服务，不读取本地 .env。"""

import asyncio
import json
import os
import re
import sys
import unittest
from contextlib import chdir
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastmcp import Client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 首次导入 Settings 时也不能加载用户的 .env。
clean_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("AETHER_")}
with TemporaryDirectory(dir=ROOT) as empty_dir, chdir(empty_dir), patch.dict(os.environ, clean_env, clear=True):
    from aether_search import channels, config, server
    from aether_search.channel_types import AnswerResult
    from aether_search.connectors.base import Source
    from aether_search.grok import GrokChannel, GrokOptions
    from aether_search.sources import SourcesCache


def completion(text):
    events = [
        {"id": "test", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        {"id": "test", "object": "chat.completion.chunk", "created": 1, "model": "test",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    body = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def tool_text(result):
    return "\n".join(item.text for item in result.content if hasattr(item, "text"))


class TestGrokIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clean = {key: value for key, value in os.environ.items() if not key.upper().startswith("AETHER_")}
        self.enterContext(patch.dict(os.environ, clean, clear=True))
        settings = config.Settings(_env_file=None, verify=False, fallback_backends="")
        self.enterContext(patch.object(config, "settings", settings))
        self.enterContext(patch.object(server, "settings", settings))
        self.enterContext(patch.object(channels, "settings", settings))
        self.enterContext(patch.object(server, "_SOURCES_CACHE", SourcesCache()))
        self.enterContext(patch.object(
            httpx.AsyncHTTPTransport, "handle_async_request",
            side_effect=AssertionError("离线测试不得访问外部 HTTP 服务"),
        ))

    def test_imports_use_new_single_implementation(self):
        self.assertIs(channels.GrokChannel, GrokChannel)
        self.assertIs(server.GrokChannel, GrokChannel)
        self.assertIs(channels.AnswerResult, AnswerResult)
        settings = config.Settings(_env_file=None, grok_provider="cpa", grok_api_format="responses",
                                   grok_request_timeout=12, grok_max_concurrency=3)
        opts = GrokOptions.from_settings(settings)
        self.assertEqual((opts.provider, opts.api_format, opts.request_timeout, opts.max_concurrency),
                         ("cpa", "responses", 12, 3))
        self.assertEqual(settings.grok_api_key, "")

    async def run_search(self, mode="success", *, parallel=False):
        grok_started, agent_started = asyncio.Event(), asyncio.Event()
        calls = []
        evidence = Source("Agent source", "https://agent.test/evidence", "Agent evidence", connector="test")

        async def seed(queries):
            agent_started.set()
            if parallel:
                await asyncio.wait_for(grok_started.wait(), timeout=2)
            return [evidence], {}

        aux = SimpleNamespace(
            primary_connectors=[object()], fallback_connectors=[],
            llm=SimpleNamespace(is_configured=lambda: False),
            run_queries=AsyncMock(side_effect=seed),
        )
        self.enterContext(patch.object(channels, "AuxAgent", return_value=aux))

        async def handler(request):
            calls.append(request)
            grok_started.set()
            if parallel:
                await asyncio.wait_for(agent_started.wait(), timeout=2)
            if mode == "payment_error":
                return httpx.Response(402, json={"error": {"message": "synthetic upstream error"}})
            if mode == "malformed":
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=b"data: {invalid json}\n\n")
            return completion("Grok answer: [Docs](https://grok.test/source)")

        opts = GrokOptions(api_url="https://mock.test/v1", api_key="synthetic-key", model="test",
                           provider="cpa", retry_max_attempts=0, timeout=5)
        if mode == "disabled":
            opts = GrokOptions()
        elif mode == "invalid_config":
            opts.endpoints = "{invalid"
        channel = GrokChannel(opts, transport=httpx.MockTransport(handler), trust_env=False)
        self.enterContext(patch.object(server, "GrokChannel", return_value=channel))
        async with Client(server.mcp) as client:
            output = tool_text(await client.call_tool("search", {"query": "offline test"}))
            session_id = re.search(r"session_id: ([a-f0-9]+)", output).group(1)
            sources = json.loads(tool_text(await client.call_tool("get_sources", {"session_id": session_id})))
        self.assertTrue(agent_started.is_set())
        aux.run_queries.assert_awaited_once()
        return output, sources, calls

    async def test_parallel_grok_primary_and_shared_sources(self):
        output, sources, calls = await self.run_search(parallel=True)
        self.assertTrue(output.startswith("Grok answer:"))
        self.assertIn("辅助证据", output)
        self.assertEqual(len(calls), 1)
        self.assertEqual({source["url"] for source in sources["sources"]},
                         {"https://grok.test/source", "https://agent.test/evidence"})

    async def test_http402_falls_back_to_agent_without_retry(self):
        output, sources, calls = await self.run_search("payment_error")
        self.assertIn("Agent evidence", output)
        self.assertIn("FAIL(HTTP 402)", output)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sources["sources_count"], 1)

    async def test_malformed_grok_does_not_break_agent(self):
        output, _, calls = await self.run_search("malformed")
        self.assertIn("Agent evidence", output)
        self.assertIn("FAIL(", output)
        self.assertEqual(len(calls), 1)

    async def test_disabled_grok_keeps_agent_available(self):
        output, _, calls = await self.run_search("disabled")
        self.assertIn("Agent evidence", output)
        self.assertNotIn("grok:", output)
        self.assertEqual(calls, [])

    async def test_invalid_grok_config_keeps_agent_available(self):
        output, _, calls = await self.run_search("invalid_config")
        self.assertIn("Agent evidence", output)
        self.assertIn("FAIL(config:", output)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
