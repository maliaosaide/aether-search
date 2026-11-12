"""图片检索后端与 search_images 工具的离线测试；不读取 .env，不访问外部服务。"""

import json
import os
import sys
import unittest
from contextlib import chdir
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from fastmcp import Client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
clean_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("AETHER_")}
with TemporaryDirectory(dir=ROOT) as empty_dir, chdir(empty_dir), patch.dict(os.environ, clean_env, clear=True):
    from aether_search import config, server
    from aether_search.connectors.brave import BraveConnector
    from aether_search.connectors.firecrawl import FirecrawlConnector
    from aether_search.connectors.linkup import LinkupConnector
    from aether_search.connectors.parallel import ParallelConnector
    from aether_search.connectors.serper import SerperConnector
    from aether_search.connectors.tavily import TavilyConnector


def tool_text(result):
    return "\n".join(item.text for item in result.content if hasattr(item, "text"))


class ImageTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, clean_env, clear=True))
        defaults = config.Settings(_env_file=None)
        for name in config.Settings.model_fields:
            self.enterContext(patch.object(config.settings, name, getattr(defaults, name)))
        config.settings.verify = False
        self.requests = []
        self.responses = []
        self.route = {}

        def handler(request):
            self.requests.append(request)
            for marker, response in self.route.items():
                if marker in str(request.url):
                    return response
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

    async def test_serper_images(self):
        config.settings.serper_api_key = "synthetic-serper-key"
        self.responses = [httpx.Response(200, json={"images": [
            {"title": "Cat", "imageUrl": "https://img.example/cat.jpg",
             "link": "https://page.example/cat", "thumbnailUrl": "https://img.example/cat-thumb.jpg",
             "imageWidth": 800, "imageHeight": 600},
            {"title": "No image"},
        ]})]
        result = await SerperConnector().search_images("cat", top_k=5)

        self.assertIsNone(result.error)
        self.assertEqual(str(self.requests[0].url), "https://google.serper.dev/images")
        self.assertEqual(json.loads(self.requests[0].content), {"q": "cat", "num": 5})
        self.assertEqual(len(result.images), 1)
        image = result.images[0]
        self.assertEqual((image.title, image.image_url, image.source_url, image.thumbnail_url,
                          image.width, image.height, image.connector),
                         ("Cat", "https://img.example/cat.jpg", "https://page.example/cat",
                          "https://img.example/cat-thumb.jpg", 800, 600, "serper"))

    async def test_http_error_includes_response_body(self):
        config.settings.serper_api_key = "synthetic-serper-key"
        self.responses = [httpx.Response(400, json={"message": "Not enough credits", "statusCode": 400})]
        result = await SerperConnector().search_images("cat")

        self.assertEqual(result.images, [])
        self.assertIn("400 Bad Request", result.error)
        self.assertIn("Not enough credits", result.error)

    async def test_brave_images(self):
        config.settings.brave_api_key = "synthetic-brave-key"
        self.responses = [httpx.Response(200, json={"results": [{
            "title": "Cat", "url": "https://page.example/cat", "source": "example.com",
            "thumbnail": {"src": "https://thumb.example/cat.jpg", "width": 500, "height": 400},
            "properties": {"url": "https://img.example/cat.jpg", "width": 1200, "height": 900},
        }]})]
        result = await BraveConnector().search_images("cat", top_k=5)

        self.assertIsNone(result.error)
        self.assertEqual(str(self.requests[0].url),
                         "https://api.search.brave.com/res/v1/images/search?q=cat&count=5")
        self.assertEqual(self.requests[0].headers["X-Subscription-Token"], "synthetic-brave-key")
        image = result.images[0]
        self.assertEqual((image.image_url, image.source_url, image.thumbnail_url,
                          image.width, image.height),
                         ("https://img.example/cat.jpg", "https://page.example/cat",
                          "https://thumb.example/cat.jpg", 1200, 900))

    async def test_parallel_images(self):
        config.settings.parallel_api_key = "synthetic-parallel-key"
        self.responses = [httpx.Response(200, json={"results": [{
            "title": "Cat", "image_url": "https://img.example/cat.jpg",
            "source_page_url": "https://page.example/cat", "width": 1200, "height": 800,
        }]})]
        result = await ParallelConnector().search_images("cat", top_k=5)

        self.assertIsNone(result.error)
        self.assertEqual(str(self.requests[0].url), "https://api.parallel.ai/v1/images/search")
        self.assertEqual(self.requests[0].headers["x-api-key"], "synthetic-parallel-key")
        self.assertNotIn("Authorization", self.requests[0].headers)
        self.assertEqual(json.loads(self.requests[0].content)["search_queries"], ["cat"])
        self.assertEqual(json.loads(self.requests[0].content)["advanced_settings"],
                         {"max_results": 5})
        self.assertEqual(result.images[0].image_url, "https://img.example/cat.jpg")

    async def test_tavily_images(self):
        config.settings.tavily_api_key = "synthetic-tavily-key"
        self.responses = [httpx.Response(200, json={"images": [
            {"url": "https://img.example/cat.jpg", "description": "A cat"},
            "https://img.example/cat2.jpg",
        ]})]
        result = await TavilyConnector().search_images("cat", top_k=5)

        self.assertIsNone(result.error)
        body = json.loads(self.requests[0].content)
        self.assertTrue(body["include_images"])
        self.assertTrue(body["include_image_descriptions"])
        self.assertEqual(body["max_results"], 5)
        self.assertEqual(len(result.images), 2)
        self.assertEqual(result.images[0].title, "A cat")
        self.assertEqual(result.images[1].image_url, "https://img.example/cat2.jpg")

    async def test_tavily_clamps_max_results_and_survives_null_description(self):
        config.settings.tavily_api_key = "synthetic-tavily-key"
        self.responses = [httpx.Response(200, json={"images": [
            {"url": "https://img.example/cat.jpg", "description": None},
        ]})]
        result = await TavilyConnector().search_images("cat", top_k=30)

        self.assertIsNone(result.error)
        self.assertEqual(json.loads(self.requests[0].content)["max_results"], 20)
        self.assertEqual(result.images[0].title, "")

    async def test_firecrawl_images(self):
        config.settings.firecrawl_api_key = "synthetic-firecrawl-key"
        self.responses = [httpx.Response(200, json={"data": {"images": [{
            "title": "Cat", "imageUrl": "https://img.example/cat.jpg",
            "imageWidth": 640, "imageHeight": 480, "url": "https://page.example/cat",
        }]}})]
        result = await FirecrawlConnector().search_images("cat", top_k=5)

        self.assertIsNone(result.error)
        self.assertEqual(str(self.requests[0].url), "https://api.firecrawl.dev/v2/search")
        self.assertEqual(json.loads(self.requests[0].content)["sources"], ["images"])
        self.assertEqual(result.images[0].source_url, "https://page.example/cat")

    async def test_linkup_filters_text_results(self):
        config.settings.linkup_api_key = "synthetic-linkup-key"
        self.responses = [httpx.Response(200, json={"results": [
            {"type": "image", "name": "Cat", "url": "https://img.example/cat.jpg"},
            {"type": "text", "name": "Doc", "url": "https://page.example/doc", "content": "text"},
        ]})]
        result = await LinkupConnector().search_images("cat", top_k=5)

        self.assertIsNone(result.error)
        body = json.loads(self.requests[0].content)
        self.assertTrue(body["includeImages"])
        self.assertEqual(body["maxResults"], 20)
        self.assertEqual(len(result.images), 1)
        self.assertEqual(result.images[0].title, "Cat")

    async def test_tool_merges_round_robin_and_dedupes(self):
        config.settings.serper_api_key = "synthetic-serper-key"
        config.settings.brave_api_key = "synthetic-brave-key"
        self.route = {
            "brave": httpx.Response(200, json={"results": [
                {"title": "A", "url": "https://page.example/a",
                 "thumbnail": {"src": "https://thumb.example/a.jpg"},
                 "properties": {"url": "https://img.example/a.jpg", "width": 10, "height": 10}},
                {"title": "C", "url": "https://page.example/c",
                 "properties": {"url": "https://img.example/c.jpg"}},
            ]}),
            "serper": httpx.Response(200, json={"images": [
                {"title": "A", "imageUrl": "https://img.example/a.jpg",
                 "link": "https://page.example/a"},
                {"title": "B", "imageUrl": "https://img.example/b.jpg",
                 "link": "https://page.example/b"},
            ]}),
        }

        async with Client(server.mcp) as client:
            result = await client.call_tool("search_images", {"query": "cat", "count": 3})

        text = tool_text(result)
        self.assertEqual(text.count("https://img.example/a.jpg"), 2)  # markdown image + explicit URL
        self.assertIn("https://img.example/b.jpg", text)
        self.assertIn("https://img.example/c.jpg", text)
        self.assertEqual(text.count("**A**"), 1)
        self.assertIn("backends: brave(2), serper(2)", text)

    async def test_tool_escapes_markdown_and_skips_bad_urls(self):
        config.settings.serper_api_key = "synthetic-serper-key"
        self.responses = [httpx.Response(200, json={"images": [
            {"title": "A\nB", "imageUrl": "https://img.example/a(1).jpg",
             "link": "https://page.example/a(1)"},
            {"title": "Bad", "imageUrl": ["https://img.example/bad.jpg"]},
        ]})]

        async with Client(server.mcp) as client:
            result = await client.call_tool("search_images", {"query": "cat", "count": 5})

        text = tool_text(result)
        self.assertIn("![A B](https://img.example/a%281%29.jpg)", text)
        self.assertIn("https://img.example/a(1).jpg", text)
        self.assertIn("[来源页](https://page.example/a%281%29)", text)
        self.assertNotIn("bad.jpg", text)
        self.assertIn("backends: serper(2)", text)

    async def test_tool_rejects_empty_query_and_missing_backend(self):
        async with Client(server.mcp) as client:
            empty = await client.call_tool("search_images", {"query": "  "})
            missing = await client.call_tool("search_images", {"query": "cat"})
        self.assertIn("query 不能为空", tool_text(empty))
        self.assertIn("未配置支持图片检索的后端", tool_text(missing))
