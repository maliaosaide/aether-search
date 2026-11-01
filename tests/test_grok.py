"""GrokChannel 离线测试:注入 GrokOptions 与 httpx.MockTransport,零真实网络。

测试经 SDK 实际请求路径驱动(MockTransport 截获),覆盖:三种接入形态的请求
形状、来源收集、成功/失败/截断判定、重试/超时/failover、race 与取消、配置
校验与脱敏。不读取 .env,不加载 dotenv,不发任何真实请求。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# 导入被测模块前清除 AETHER_* 环境变量,隔离本机配置(也不读取 .env)
for _name in [k for k in os.environ if k.upper().startswith("AETHER_")]:
    del os.environ[_name]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

import aether_search.grok as grok  # noqa: E402
from aether_search.channel_types import AnswerResult  # noqa: E402
from aether_search.grok import GrokChannel, GrokOptions  # noqa: E402

# ---------------------------------------------------------------------------
# 响应构造辅助(匹配 SDK 公共模型的最小合法 payload)
# ---------------------------------------------------------------------------


def chat_chunk(content: str | None = None, finish: str | None = None, index: int = 0) -> dict:
    choice: dict = {"index": index, "delta": {"content": content}, "finish_reason": finish}
    return {
        "id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
        "choices": [choice],
    }


def chat_sse(*chunks: dict, done: bool = True) -> bytes:
    lines = ["data: " + json.dumps(chunk) for chunk in chunks]
    if done:
        lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def response_obj(status: str = "completed", output: list | None = None) -> dict:
    return {
        "id": "r1", "object": "response", "created_at": 1, "status": status,
        "model": "m", "output": output or [], "error": None,
    }


def text_item(text: str, annotations: list | None = None, role: str = "assistant") -> dict:
    part: dict = {"type": "output_text", "text": text, "annotations": annotations or []}
    return {"id": "m1", "type": "message", "status": "completed", "role": role, "content": [part]}


def resp_completed(output: list | None = None, status: str = "completed") -> dict:
    return {"type": "response.completed", "response": response_obj(status, output)}


def responses_sse(*events: dict) -> bytes:
    return b"".join(
        ("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()
        for event in events
    )


def sse_response(payload: bytes) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload)


def error_response(status: int, retry_after: str | None = None) -> httpx.Response:
    headers = {"retry-after": retry_after} if retry_after else {}
    return httpx.Response(status, headers=headers, json={"error": {"message": "upstream secret"}})


async def _slow_body(payload: bytes, delay: float):
    await asyncio.sleep(delay)
    yield payload


def hanging_sse(delay: float = 10.0) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=_slow_body(chat_sse(chat_chunk("late"), chat_chunk(finish="stop")), delay),
    )


def make_transport(plan: list):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if not plan:
            return httpx.Response(500, text="plan exhausted")
        item = plan.pop(0)
        return item(request) if callable(item) and not isinstance(item, httpx.Response) else item

    return httpx.MockTransport(handler), calls


def options(**overrides) -> GrokOptions:
    base = dict(
        api_url="https://api.openai.example/v1",
        api_key="sk-test-key",
        model="gpt-test",
        retry_max_attempts=0,
        timeout=10.0,
        request_timeout=5.0,
    )
    base.update(overrides)
    return GrokOptions(**base)


def endpoints_json(*urls: str, **common) -> str:
    entries = [dict({"url": url, "key": "k", "model": "m"}, **common) for url in urls]
    return json.dumps(entries)


# ---------------------------------------------------------------------------
# 数据契约与配置
# ---------------------------------------------------------------------------


class TestAnswerResultContract(unittest.TestCase):
    def test_positional_field_order(self):
        result = AnswerResult("grok", "text", ["u"], [], "detail", 0.5, None)
        self.assertEqual(
            (result.channel, result.text, result.citations, result.evidence,
             result.detail, result.elapsed, result.error),
            ("grok", "text", ["u"], [], "detail", 0.5, None),
        )


class TestConfig(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_configs_never_crash_and_report_sanitized(self):
        cases = [
            options(provider=None),
            options(provider="weird"),
            options(api_format="weird"),
            options(search_mode="weird"),
            options(failover="parallel"),
            options(timeout="fast"),
            options(timeout=0),
            options(timeout=float("inf")),
            options(request_timeout=-1),
            options(max_concurrency=0),
            options(retry_max_attempts=-1),
            options(endpoints="{not json"),
            options(endpoints="[]"),
            options(endpoints='["str"]'),
            options(api_url="", endpoints=endpoints_json("https://a.test/v1", label="x\ny")),
            options(api_url="", endpoints=endpoints_json("https://a.test/v1", label="L" * 100)),
            options(api_url="", endpoints=endpoints_json("https://a.test/v1", provider=7)),
            options(api_url="", endpoints=endpoints_json("https://a.test/v1", search_mode=[])),
            options(api_url="https://host:abc/v1"),
            options(api_url="ftp://host/v1"),
            options(api_url="https://user:pw@host/v1"),
        ]
        for opts in cases:
            with self.subTest(case=repr(opts)):
                channel = GrokChannel(opts)  # 不得抛出
                self.assertEqual(channel.endpoints, [])
                self.assertTrue(channel.enabled())  # 显式配置但无效 → True,由 answer 报错
                result = await channel.answer("q")
                self.assertIsNotNone(result.error)
                self.assertEqual(result.text, "")
                self.assertIn("config", result.error)

    async def test_url_only_without_key_disabled(self):
        channel = GrokChannel(options(api_key=""))
        self.assertFalse(channel.enabled())
        result = await channel.answer("q")
        self.assertIn("key is missing", result.error)  # answer 仍可给出脱敏原因

    async def test_unconfigured_defaults_disabled(self):
        result = await GrokChannel(GrokOptions()).answer("q")
        self.assertIn("not configured", result.error)

    async def test_xai_explicit_chat_rejected_and_search_on_chat_rejected(self):
        self.assertIn("xai", (await GrokChannel(options(provider="xai", api_format="chat_completions")).answer("q")).error)
        self.assertIn("search_mode", (await GrokChannel(options(search_mode="on")).answer("q")).error)

    async def test_search_mode_empty_overrides_global_on(self):
        opts = options(
            api_url="",
            endpoints=endpoints_json("https://one.example/v1", search_mode=""),
            search_mode="on",
        )
        transport, calls = make_transport([
            sse_response(chat_sse(chat_chunk("ok"), chat_chunk(finish="stop")))
        ])
        channel = GrokChannel(opts, transport=transport)
        result = await channel.answer("q")
        self.assertIsNone(result.error)  # 端点 '' 覆盖了全局 on,不再配置错误
        self.assertEqual(len(calls), 1)

    async def test_endpoints_json_overrides_single_with_inheritance(self):
        opts = options(
            api_url="https://ignored.test/v1",
            endpoints=json.dumps([
                {"url": "https://one.test/v1", "model": "alias-1", "label": "first"},
                {"url": "https://two.test/v1"},
            ]),
        )
        channel = GrokChannel(opts)
        eps = channel.endpoints
        self.assertEqual([ep.url for ep in eps], ["https://one.test/v1", "https://two.test/v1"])
        self.assertEqual(eps[0].model, "alias-1")
        self.assertEqual(eps[0].label, "first")
        self.assertEqual(eps[1].key, "sk-test-key")  # key 继承全局
        self.assertEqual(eps[1].label, "endpoint-2")

    async def test_repr_and_errors_never_leak_key(self):
        secret = "sk-super-secret"
        transport, calls = make_transport([error_response(401)])
        channel = GrokChannel(options(api_key=secret), transport=transport)
        result = await channel.answer("q")
        self.assertNotIn(secret, repr(channel.options) + result.error + result.detail)
        for endpoint in channel.endpoints:
            self.assertNotIn(secret, repr(endpoint))
        self.assertIn("HTTP 401", result.error)

    async def test_upstream_error_text_not_echoed(self):
        transport, _calls = make_transport([error_response(400)])
        channel = GrokChannel(options(), transport=transport)
        result = await channel.answer("q")
        self.assertEqual(result.error, "HTTP 400")  # 状态码之外不回显响应体
        self.assertNotIn("upstream secret", result.error + result.detail)

    async def test_lazy_settings_read_via_fake_config(self):
        fake = types.ModuleType("aether_search.config")
        fake.settings = types.SimpleNamespace(
            grok_api_url="https://lazy.test/v1", grok_api_key="lazy-key", grok_model="lazy-model"
        )
        with patch.dict(sys.modules, {"aether_search.config": fake}), patch.object(
            sys.modules["aether_search"], "config", fake, create=True
        ):
            channel = GrokChannel()  # 不带 options,惰性读取 config.settings
            self.assertTrue(channel.enabled())
            self.assertEqual(channel.endpoints[0].url, "https://lazy.test/v1")
            self.assertEqual(channel.endpoints[0].key, "lazy-key")

    async def test_settings_import_failure_reportable(self):
        class Boom:
            def __getattr__(self, name):
                raise RuntimeError("boom")

        broken = Boom()
        with patch.dict(sys.modules, {"aether_search.config": broken}), patch.object(
            sys.modules["aether_search"], "config", broken, create=True
        ):
            channel = GrokChannel()
            result = await channel.answer("q")
            self.assertEqual(result.error, "config: settings unavailable")  # 与未配置区分


class TestFromSettings(unittest.TestCase):
    def test_full_and_missing_fields(self):
        full = types.SimpleNamespace(
            grok_api_url="https://api.x.ai/v1", grok_api_key="k", grok_model="grok-4",
            grok_provider="xai", grok_api_format="responses", grok_search_mode="on",
            grok_failover="race", grok_timeout=12.5, grok_request_timeout=8.0,
            grok_max_concurrency=3, retry_max_attempts=1,
        )
        opts = GrokOptions.from_settings(full)
        self.assertEqual((opts.api_url, opts.model, opts.failover, opts.timeout), (
            "https://api.x.ai/v1", "grok-4", "race", 12.5))
        minimal = GrokOptions.from_settings(
            types.SimpleNamespace(grok_api_url="https://g.test/v1", grok_api_key="k", grok_model="m")
        )
        self.assertEqual((minimal.failover, minimal.timeout, minimal.retry_max_attempts),
                         ("sequential", 60.0, 3))
        defaults = GrokOptions.from_settings(None)
        self.assertEqual(defaults.api_url, "")

    def test_invalid_typed_values_surface_not_silent_defaults(self):
        opts = GrokOptions.from_settings(
            types.SimpleNamespace(grok_api_url="https://g.test/v1", grok_api_key="k",
                                  grok_model="m", grok_timeout="fast")
        )
        channel = GrokChannel(opts)
        self.assertEqual(channel.endpoints, [])  # 非法类型不变成静默默认值
        result = asyncio.run(channel.answer("q"))
        self.assertIn("positive finite", result.error)


# ---------------------------------------------------------------------------
# 请求形状:三种接入形态
# ---------------------------------------------------------------------------


class TestRequestShapes(unittest.IsolatedAsyncioTestCase):
    async def test_openai_chat_request_and_answer(self):
        transport, calls = make_transport([
            sse_response(chat_sse(
                chat_chunk("Answer: see [docs](https://d.test/x)."),
                chat_chunk(finish="stop"),
            ))
        ])
        channel = GrokChannel(options(), transport=transport)
        result = await channel.answer("hello", platform="github")
        self.assertIsNone(result.error)
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertEqual(str(request.url), "https://api.openai.example/v1/chat/completions")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["authorization"], "Bearer sk-test-key")
        body = json.loads(request.content)
        self.assertEqual(body["model"], "gpt-test")
        self.assertEqual(body["stream"], True)
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertIn("platform: github", body["messages"][1]["content"])
        for absent in ("max_tokens", "web_search_options", "tools", "temperature"):
            self.assertNotIn(absent, body)
        self.assertEqual(result.channel, "grok")
        self.assertEqual(result.evidence, [])
        self.assertIn("provider=openai", result.detail)
        self.assertIn("format=chat_completions", result.detail)
        self.assertIn("attempts=1", result.detail)

    async def test_cpa_custom_prefix_and_model_alias(self):
        transport, calls = make_transport([
            sse_response(chat_sse(chat_chunk("CPA answer"), chat_chunk(finish="stop")))
        ])
        opts = options(
            api_url="",
            endpoints=json.dumps([{"url": "https://gw.internal/api/v1", "key": "cpa-key",
                                   "model": "my-alias@v2", "label": "cpa", "provider": "cpa"}]),
        )
        channel = GrokChannel(opts, transport=transport)
        result = await channel.answer("q")
        self.assertIsNone(result.error)
        self.assertEqual(str(calls[0].url), "https://gw.internal/api/v1/chat/completions")
        self.assertEqual(calls[0].headers["authorization"], "Bearer cpa-key")
        self.assertEqual(json.loads(calls[0].content)["model"], "my-alias@v2")  # 别名原样
        self.assertIn("provider=cpa", result.detail)

    async def test_xai_native_responses_tools_and_store(self):
        transport, calls = make_transport([
            sse_response(responses_sse(
                {"type": "response.web_search_call.completed", "output_index": 1, "item_id": "ws"},
                resp_completed([{"type": "web_search_call", "id": "ws", "status": "completed"},
                                text_item("Searched answer", annotations=[
                                    {"type": "url_citation", "url": "https://d.test/x", "title": "D",
                                     "start_index": 0, "end_index": 3}])]),
            ))
        ])
        channel = GrokChannel(options(api_url="https://api.x.ai/v1", model="grok-4"), transport=transport)
        result = await channel.answer("q")
        self.assertIsNone(result.error)
        request = calls[0]
        self.assertEqual(str(request.url), "https://api.x.ai/v1/responses")
        body = json.loads(request.content)
        self.assertEqual(body["tools"], [{"type": "web_search"}, {"type": "x_search"}])
        self.assertEqual(body["store"], False)
        self.assertEqual(body["stream"], True)
        self.assertIn("provider=xai", result.detail)
        self.assertIn("search=observed", result.detail)
        self.assertEqual(result.citations, ["https://d.test/x"])
        self.assertIn("Sources:", result.text)

    async def test_generic_responses_explicit_no_speculative_tools(self):
        transport, calls = make_transport([
            sse_response(responses_sse(resp_completed([text_item("Resp answer")])))
        ])
        channel = GrokChannel(options(api_format="responses"), transport=transport)
        result = await channel.answer("q")
        self.assertIsNone(result.error)
        self.assertEqual(str(calls[0].url), "https://api.openai.example/v1/responses")
        self.assertNotIn("tools", json.loads(calls[0].content))
        self.assertIn("search=not_requested", result.detail)

    async def test_hostname_detection_not_substring(self):
        transport, calls = make_transport([
            sse_response(chat_sse(chat_chunk("A"), chat_chunk(finish="stop")))
        ])
        channel = GrokChannel(options(api_url="https://api.x.ai.evil.com/v1"), transport=transport)
        result = await channel.answer("q")
        self.assertIsNone(result.error)
        self.assertTrue(str(calls[0].url).endswith("/chat/completions"))
        self.assertIn("provider=openai", result.detail)


# ---------------------------------------------------------------------------
# 来源收集与失败判定
# ---------------------------------------------------------------------------


class TestSourcesAndFailures(unittest.IsolatedAsyncioTestCase):
    async def test_final_text_inline_and_bare_links_collected(self):
        transport, _calls = make_transport([
            sse_response(chat_sse(
                chat_chunk("See [Docs](https://a.test/docs) and https://b.test/x."),
                chat_chunk(finish="stop"),
            ))
        ])
        channel = GrokChannel(options(), transport=transport)
        result = await channel.answer("q")
        self.assertIsNone(result.error)
        self.assertEqual(result.citations, ["https://a.test/docs", "https://b.test/x"])
        self.assertIn("Sources:", result.text)
        self.assertIn("- [Docs](https://a.test/docs)", result.text)

    async def test_structured_only_citations_survive_in_footer(self):
        transport, _calls = make_transport([
            sse_response(responses_sse(resp_completed([text_item(
                "Plain answer without links",
                annotations=[{"type": "url_citation", "url": "https://s.test/only", "title": "Only",
                              "start_index": 0, "end_index": 3}])])))
        ])
        channel = GrokChannel(options(api_format="responses"), transport=transport)
        result = await channel.answer("q")
        self.assertIsNone(result.error)
        self.assertEqual(result.citations, ["https://s.test/only"])
        self.assertIn("- [Only](https://s.test/only)", result.text)

    async def test_mixed_structured_and_plain_urls_dedupe(self):
        transport, _calls = make_transport([
            sse_response(responses_sse(resp_completed([text_item(
                "See https://a.test/x and [A](https://a.test/x)",
                annotations=[{"type": "url_citation", "url": "https://a.test/x", "title": "A Title",
                              "start_index": 0, "end_index": 3}])])))
        ])
        channel = GrokChannel(options(api_format="responses"), transport=transport)
        result = await channel.answer("q")
        self.assertEqual(result.citations, ["https://a.test/x"])  # 去重,标题保留注解版
        self.assertIn("- [A Title](https://a.test/x)", result.text)

    async def test_parenthesized_url_safe_roundtrip(self):
        wiki = "https://en.wikipedia.org/wiki/Python_(programming_language)"
        encoded = "https://en.wikipedia.org/wiki/Python_%28programming_language%29"
        transport, _calls = make_transport([
            sse_response(chat_sse(chat_chunk(f"See [{wiki}]({wiki})."), chat_chunk(finish="stop")))
        ])
        channel = GrokChannel(options(), transport=transport)
        result = await channel.answer("q")
        self.assertEqual(result.citations, [encoded])  # citations 与页脚一致的安全编码
        self.assertIn(encoded, result.text)

    async def test_truncation_and_filter_not_success_then_fallback(self):
        for finish in ("length", "content_filter"):
            with self.subTest(finish=finish):
                transport, calls = make_transport([
                    sse_response(chat_sse(chat_chunk("partial"), chat_chunk(finish=finish))),
                    sse_response(chat_sse(chat_chunk("full"), chat_chunk(finish="stop"))),
                ])
                opts = options(api_url="", endpoints=endpoints_json(
                    "https://one.test/v1", "https://two.test/v1"))
                channel = GrokChannel(opts, transport=transport)
                result = await channel.answer("q")
                self.assertIsNone(result.error)
                self.assertIn("full", result.text)
                self.assertEqual(len(calls), 2)

    async def test_dropped_stream_without_stop_fails(self):
        transport, _calls = make_transport([sse_response(chat_sse(chat_chunk("partial"), done=False))])
        channel = GrokChannel(options(), transport=transport)
        result = await channel.answer("q")
        self.assertIsNotNone(result.error)
        self.assertEqual(result.text, "")

    async def test_error_payload_mid_stream_not_success(self):
        raw = (
            b"data: " + json.dumps(chat_chunk("partial")).encode() + b"\n\n"
            + b"data: " + json.dumps({"error": {"code": "upstream_failure", "message": "SECRET_X"}}).encode() + b"\n\n"
            + b"data: [DONE]\n\n"
        )
        transport, _calls = make_transport([sse_response(raw)])
        channel = GrokChannel(options(), transport=transport)
        result = await channel.answer("q")
        self.assertIsNotNone(result.error)
        self.assertEqual(result.text, "")
        self.assertNotIn("SECRET_X", result.error + result.detail)

    async def test_multiple_choices_not_concatenated(self):
        chunk = {
            "id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
            "choices": [
                {"index": 0, "delta": {"content": "primary"}, "finish_reason": None},
                {"index": 1, "delta": {"content": "alternative"}, "finish_reason": None},
            ],
        }
        transport, _calls = make_transport([
            sse_response(chat_sse(chunk, chat_chunk(finish="stop")))
        ])
        channel = GrokChannel(options(), transport=transport)
        result = await channel.answer("q")
        self.assertIsNone(result.error)
        self.assertIn("primary", result.text)
        self.assertNotIn("alternative", result.text)

    async def test_responses_status_not_completed_is_failure(self):
        for status in ("in_progress", "failed", "incomplete", "cancelled"):
            with self.subTest(status=status):
                transport, calls = make_transport([
                    sse_response(responses_sse(resp_completed([text_item("partial text")], status=status))),
                    sse_response(chat_sse(chat_chunk("fallback"), chat_chunk(finish="stop"))),
                ])
                opts = options(
                    api_url="",
                    endpoints=json.dumps([
                        {"url": "https://one.test/v1", "key": "k", "model": "m", "api_format": "responses"},
                        {"url": "https://two.test/v1", "key": "k", "model": "m"},
                    ]),
                )
                channel = GrokChannel(opts, transport=transport)
                result = await channel.answer("q")
                self.assertIsNone(result.error)
                self.assertIn("fallback", result.text)
                self.assertEqual(len(calls), 2)

    async def test_reasoning_never_becomes_answer(self):
        output = [
            {"type": "reasoning", "id": "rs_1", "summary": [],
             "content": [{"type": "reasoning_text", "text": "SECRET_REASONING"}]},
            text_item("Final public answer"),
        ]
        transport, _calls = make_transport([sse_response(responses_sse(resp_completed(output)))])
        channel = GrokChannel(options(api_format="responses"), transport=transport)
        result = await channel.answer("q")
        self.assertIsNone(result.error)
        self.assertIn("Final public answer", result.text)
        self.assertNotIn("SECRET_REASONING", result.text)

    async def test_responses_error_event_sanitized(self):
        transport, _calls = make_transport([
            sse_response(responses_sse(
                {"type": "error", "code": "SECRET_CODE", "message": "SECRET_MSG", "param": None}))
        ])
        channel = GrokChannel(options(api_format="responses"), transport=transport)
        result = await channel.answer("q")
        self.assertIsNotNone(result.error)
        self.assertNotIn("SECRET_CODE", result.error + result.detail)
        self.assertNotIn("SECRET_MSG", result.error + result.detail)

    async def test_responses_without_completion_signal_fails(self):
        transport, _calls = make_transport([
            sse_response(responses_sse({"type": "response.created", "response": response_obj("in_progress")}))
        ])
        channel = GrokChannel(options(api_format="responses"), transport=transport)
        result = await channel.answer("q")
        self.assertIsNotNone(result.error)
        self.assertEqual(result.text, "")


class TestReviewRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_responses_collects_text_and_top_level_sources(self):
        event = resp_completed([text_item("See https://plain.test/docs")])
        event["response"]["citations"] = ["https://top.test/source"]
        transport, _ = make_transport([sse_response(responses_sse(event))])
        result = await GrokChannel(options(api_format="responses"), transport=transport).answer("q")
        self.assertIsNone(result.error)
        self.assertEqual(set(result.citations), {"https://plain.test/docs", "https://top.test/source"})

    async def test_chat_collects_top_level_sources(self):
        chunk = chat_chunk("Answer")
        chunk["citations"] = ["https://top.test/chat"]
        transport, _ = make_transport([
            sse_response(chat_sse(chunk, chat_chunk(finish="stop")))
        ])
        result = await GrokChannel(options(), transport=transport).answer("q")
        self.assertEqual(result.citations, ["https://top.test/chat"])

    async def test_responses_closes_stream_before_return(self):
        closed = asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield responses_sse(resp_completed([text_item("Answer")]))
                await asyncio.Event().wait()

            async def aclose(self):
                closed.set()

        class Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())

        result = await GrokChannel(
            options(api_format="responses"), transport=Transport()
        ).answer("q")
        self.assertIsNone(result.error)
        self.assertTrue(closed.is_set())

    async def test_malformed_stream_returns_failure(self):
        transport, calls = make_transport([sse_response(b"data: {invalid json}\n\n")])
        result = await GrokChannel(options(), transport=transport).answer("q")
        self.assertIsNotNone(result.error)
        self.assertEqual(result.text, "")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("invalid json", result.error)

    async def test_malformed_stream_uses_backup_endpoint(self):
        transport, calls = make_transport([
            sse_response(b"data: {invalid json}\n\n"),
            sse_response(chat_sse(chat_chunk("Recovered"), chat_chunk(finish="stop"))),
        ])
        opts = options(api_url="", endpoints=endpoints_json("https://one.test/v1", "https://two.test/v1"))
        result = await GrokChannel(opts, transport=transport).answer("q")
        self.assertIsNone(result.error)
        self.assertEqual(result.text, "Recovered")
        self.assertEqual(len(calls), 2)


# ---------------------------------------------------------------------------
# 重试 / 超时 / failover / race / 取消
# ---------------------------------------------------------------------------


class TestScheduling(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = (grok.RETRY_BACKOFF_BASE, grok.RETRY_BACKOFF_MAX)
        grok.RETRY_BACKOFF_BASE = 0.001
        grok.RETRY_BACKOFF_MAX = 0.002

    def tearDown(self):
        grok.RETRY_BACKOFF_BASE, grok.RETRY_BACKOFF_MAX = self._saved

    async def test_429_retry_after_then_success(self):
        transport, calls = make_transport([
            error_response(429, retry_after="0"),
            sse_response(chat_sse(chat_chunk("after 429"), chat_chunk(finish="stop"))),
        ])
        channel = GrokChannel(options(retry_max_attempts=1), transport=transport)
        result = await channel.answer("q")
        self.assertIsNone(result.error)
        self.assertEqual(len(calls), 2)
        self.assertIn("attempts=2", result.detail)

    async def test_401_never_retried_same_endpoint(self):
        transport, calls = make_transport([error_response(401), error_response(401)])
        opts = options(api_url="", endpoints=endpoints_json("https://one.test/v1", "https://two.test/v1"),
                       retry_max_attempts=5)
        channel = GrokChannel(opts, transport=transport)
        result = await channel.answer("q")
        self.assertIn("HTTP 401", result.error)
        self.assertEqual(len(calls), 2)  # 每端点恰好一次

    async def test_shared_retry_budget_caps_requests(self):
        transport, calls = make_transport([])  # 全部 500
        opts = options(api_url="", endpoints=endpoints_json("https://one.test/v1", "https://two.test/v1"),
                       retry_max_attempts=1)
        channel = GrokChannel(opts, transport=transport)
        result = await channel.answer("q")
        self.assertIsNotNone(result.error)
        self.assertEqual(len(calls), 3)  # 端点数 + retry_max 封顶

    async def test_empty_answer_falls_back(self):
        transport, calls = make_transport([
            sse_response(chat_sse(chat_chunk(finish="stop"))),
            sse_response(chat_sse(chat_chunk("non-empty"), chat_chunk(finish="stop"))),
        ])
        opts = options(api_url="", endpoints=endpoints_json("https://one.test/v1", "https://two.test/v1"))
        channel = GrokChannel(opts, transport=transport)
        result = await channel.answer("q")
        self.assertIsNone(result.error)
        self.assertEqual(len(calls), 2)

    async def test_per_attempt_timeout(self):
        channel = GrokChannel(options(request_timeout=0.2), transport=httpx.MockTransport(
            lambda request: hanging_sse(delay=5.0)))
        result = await channel.answer("q")
        self.assertIn("timeout", result.error)
        self.assertLess(result.elapsed, 1.5)

    async def test_total_timeout_independent_of_endpoint_count(self):
        opts = options(api_url="", endpoints=endpoints_json(
            "https://h1.test/v1", "https://h2.test/v1", "https://h3.test/v1"),
            timeout=0.4, request_timeout=5.0)
        channel = GrokChannel(opts, transport=httpx.MockTransport(
            lambda request: hanging_sse(delay=10.0)))
        result = await channel.answer("q")
        self.assertIn("timeout", result.error)
        self.assertLess(result.elapsed, 2.0)

    async def test_caller_cancellation_propagates(self):
        channel = GrokChannel(options(), transport=httpx.MockTransport(
            lambda request: hanging_sse(delay=10.0)))
        task = asyncio.create_task(channel.answer("q"))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def _race_channel(self, opts, factory):
        class _T(httpx.AsyncBaseTransport):
            def __init__(self):
                self.calls = []

            async def handle_async_request(self, request):
                self.calls.append(request)
                return factory(request, len(self.calls))

        transport = _T()
        return GrokChannel(opts, transport=transport), transport

    async def test_race_first_success_cancels_loser(self):
        tracker = {"closed": 0}

        class TrackedStream(httpx.AsyncByteStream):
            def __init__(self, payload, delay):
                self._payload, self._delay = payload, delay

            async def __aiter__(self):
                await asyncio.sleep(self._delay)
                yield self._payload

            async def aclose(self):
                tracker["closed"] += 1

        payload = chat_sse(chat_chunk("fast win"), chat_chunk(finish="stop"))

        def factory(request, n):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=TrackedStream(payload, 5.0 if n == 1 else 0.0))

        opts = options(api_url="", endpoints=endpoints_json("https://r1.test/v1", "https://r2.test/v1"),
                       failover="race")
        channel, transport = await self._race_channel(opts, factory)
        result = await channel.answer("q")
        self.assertIsNone(result.error)
        self.assertIn("fast win", result.text)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(tracker["closed"], 2)  # 赢家与输家的流都被关闭

    async def test_race_semaphore_bounds_concurrency(self):
        state = {"inflight": 0, "peak": 0}

        class PeakStream(httpx.AsyncByteStream):
            def __init__(self, payload):
                self._payload = payload
                self._closed = False
                # Count live responses; the SDK adapter may iterate a stream more than once.
                state["inflight"] += 1
                state["peak"] = max(state["peak"], state["inflight"])

            async def __aiter__(self):
                await asyncio.sleep(0.05)
                yield self._payload

            async def aclose(self):
                if not self._closed:
                    self._closed = True
                    state["inflight"] -= 1

        payload = chat_sse(chat_chunk("raced"), chat_chunk(finish="stop"))

        def factory(request, n):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=PeakStream(payload))

        opts = options(api_url="", endpoints=endpoints_json(
            "https://r1.test/v1", "https://r2.test/v1", "https://r3.test/v1"),
            failover="race", max_concurrency=2)
        channel, transport = await self._race_channel(opts, factory)
        result = await channel.answer("q")
        self.assertIsNone(result.error)
        self.assertLessEqual(state["peak"], 2)
        self.assertEqual(state["peak"], 2)
        self.assertEqual(state["inflight"], 0)

    async def test_race_shared_retry_budget(self):
        attempts: list[str] = []

        def factory(request, n):
            attempts.append(request.url.host)
            return error_response(500 if request.url.host == "r1.test" else 400)

        opts = options(api_url="", endpoints=endpoints_json("https://r1.test/v1", "https://r2.test/v1"),
                       failover="race", retry_max_attempts=1)
        channel, transport = await self._race_channel(opts, factory)
        result = await channel.answer("q")
        self.assertIsNotNone(result.error)
        self.assertEqual(len(attempts), 3)  # race 同样受共享预算约束

    async def test_race_caller_cancellation_propagates(self):
        opts = options(api_url="", endpoints=endpoints_json("https://r1.test/v1", "https://r2.test/v1"),
                       failover="race")
        channel = GrokChannel(opts, transport=httpx.MockTransport(lambda request: hanging_sse()))
        task = asyncio.create_task(channel.answer("q"))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
