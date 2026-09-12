"""grok_protocol 离线单元测试:纯函数,无网络、无 SDK、无 config。"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 导入被测模块前清除 AETHER_* 环境变量,隔离本机配置(也不读取 .env)
for _name in [k for k in os.environ if k.upper().startswith("AETHER_")]:
    del os.environ[_name]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aether_search.grok_protocol import (  # noqa: E402
    SEARCH_NOT_REQUESTED,
    SEARCH_REQUESTED,
    SEARCH_UNCONFIRMED,
    CitationCollector,
    ProtocolError,
    build_system_prompt,
    build_user_prompt,
    extract_text_links,
    markdown_safe_url,
    normalize_base_url,
    parse_retry_after,
    render_sources_footer,
    resolve_api_format,
    resolve_provider,
    resolve_tools,
    initial_search_state,
)


class TestBaseUrl(unittest.TestCase):
    def test_normalize_trailing_slash(self):
        self.assertEqual(normalize_base_url("https://api.x.ai/v1"), "https://api.x.ai/v1/")
        self.assertEqual(normalize_base_url("https://api.x.ai/v1/"), "https://api.x.ai/v1/")
        self.assertEqual(normalize_base_url("https://host.example"), "https://host.example/")

    def test_rejects_bad_urls_sanitized(self):
        for bad in ("ftp://host/v1", "https:///v1", "https://user:pw@host/v1",
                    "https://host/v1?q=1", "https://host/v1#f", "https://host:abc/v1",
                    "", "  ", "//host/v1"):
            with self.subTest(bad=bad):
                with self.assertRaises(ProtocolError) as ctx:
                    normalize_base_url(bad)
                if bad.strip():
                    self.assertNotIn(bad, str(ctx.exception))

    def test_port_preserved(self):
        self.assertEqual(normalize_base_url("http://localhost:8080/v1"), "http://localhost:8080/v1/")


class TestProfiles(unittest.TestCase):
    def test_provider_exact_hostname_only(self):
        self.assertEqual(resolve_provider("https://api.x.ai/v1", "auto"), "xai")
        self.assertEqual(resolve_provider("https://API.x.AI/v1", "auto"), "xai")
        self.assertEqual(resolve_provider("https://api.x.ai.evil.com/v1", "auto"), "openai")
        self.assertEqual(resolve_provider("https://gw.local/api/v1", "auto"), "openai")
        self.assertEqual(resolve_provider("https://gw.local/api/v1", "cpa"), "cpa")

    def test_api_format_auto_and_explicit(self):
        self.assertEqual(resolve_api_format("xai", "auto"), "responses")
        self.assertEqual(resolve_api_format("openai", "auto"), "chat_completions")
        self.assertEqual(resolve_api_format("cpa", "auto"), "chat_completions")
        self.assertEqual(resolve_api_format("openai", "responses"), "responses")

    def test_tools_rules(self):
        self.assertEqual(resolve_tools("responses", "", "xai"), [{"type": "web_search"}, {"type": "x_search"}])
        self.assertEqual(resolve_tools("responses", "on", "xai"), [{"type": "web_search"}, {"type": "x_search"}])
        self.assertEqual(resolve_tools("responses", "off", "xai"), [])
        self.assertEqual(resolve_tools("responses", "", "openai"), [])
        self.assertEqual(resolve_tools("responses", "on", "cpa"), [{"type": "web_search"}])
        self.assertEqual(resolve_tools("chat_completions", "", "openai"), [])
        with self.assertRaises(ProtocolError):
            resolve_tools("chat_completions", "on", "openai")

    def test_search_state(self):
        self.assertEqual(initial_search_state("chat_completions", "", "openai"), SEARCH_UNCONFIRMED)
        self.assertEqual(initial_search_state("chat_completions", "off", "openai"), SEARCH_NOT_REQUESTED)
        self.assertEqual(initial_search_state("responses", "", "xai"), SEARCH_REQUESTED)
        self.assertEqual(initial_search_state("responses", "", "openai"), SEARCH_NOT_REQUESTED)


class TestPrompts(unittest.TestCase):
    def test_system_prompt(self):
        prompt = build_system_prompt(now=datetime(2026, 9, 12, 8, 30, tzinfo=timezone.utc))
        self.assertIn("2026-09-12 08:30 UTC", prompt)
        self.assertIn("same language", prompt)
        self.assertIn("untrusted data", prompt)
        self.assertIn("Never fabricate", prompt)

    def test_user_prompt_platform(self):
        self.assertEqual(build_user_prompt("  q "), "q")
        self.assertEqual(build_user_prompt("q", "github"), "q\n\nWhere relevant, focus on the platform: github.")


class TestTextLinks(unittest.TestCase):
    def test_markdown_and_bare_with_label(self):
        urls = extract_text_links("See [Docs](https://a.test/docs) and https://b.test/x, plus ftp://skip.")
        self.assertEqual(urls, [("https://a.test/docs", "Docs"), ("https://b.test/x", None)])

    def test_url_as_label_not_used_as_title(self):
        wiki = "https://en.wikipedia.org/wiki/Python_(programming_language)"
        self.assertEqual(extract_text_links(f"See [{wiki}]({wiki})."), [(wiki, None)])

    def test_balanced_parentheses_preserved(self):
        urls = extract_text_links("Visit https://en.wikipedia.org/wiki/Python_(programming_language) now")
        self.assertEqual(urls, [("https://en.wikipedia.org/wiki/Python_(programming_language)", None)])

    def test_chinese_and_ascii_trailing_punct_stripped(self):
        self.assertEqual(extract_text_links("参考 https://a.test/p。"), [("https://a.test/p", None)])
        self.assertEqual(extract_text_links("see https://a.test/x)."), [("https://a.test/x", None)])

    def test_no_phantom_or_truncated(self):
        self.assertEqual(extract_text_links("普通回答,没有链接。"), [])
        self.assertEqual(extract_text_links("contact http://"), [])


class TestCitations(unittest.TestCase):
    def test_dedupe_and_titles(self):
        collector = CitationCollector()
        collector.add("https://a.test")
        collector.add("https://a.test", "A 标题")
        collector.add("https://b.test", "B")
        self.assertEqual(collector.items(), [("https://a.test", "A 标题"), ("https://b.test", "B")])

    def test_invalid_urls_skipped(self):
        collector = CitationCollector()
        for bad in ("ftp://x.test", "javascript:alert(1)", "http://", None, 42, "https://a.test/x y"):
            collector.add(bad)
        self.assertEqual(collector.items(), [])

    def test_annotation_shapes_flat_nested_object(self):
        collector = CitationCollector()
        collector.add_annotation({"type": "url_citation", "url": "https://flat.test", "title": "F"})
        collector.add_annotation({"type": "url_citation", "url_citation": {"url": "https://nested.test", "title": "N"}})
        collector.add_annotation(None)
        collector.add_annotation({"type": "other", "url": "https://skip.test"})

        class _Ann:
            url = "https://obj.test"
            title = "O"

        collector.add_annotation(_Ann())

        class _Nested:
            class url_citation:  # noqa: N801 - 模拟 SDK 嵌套对象
                url = "https://objnested.test"
                title = "ON"

            url = None
            title = None

        collector.add_annotation(_Nested())
        self.assertEqual(collector.items(), [
            ("https://flat.test", "F"), ("https://nested.test", "N"),
            ("https://obj.test", "O"), ("https://objnested.test", "ON"),
        ])

    def test_footer_safe_encoding_roundtrip(self):
        items = [("https://en.wikipedia.org/wiki/Python_(programming_language)", "Wiki")]
        footer = render_sources_footer(items)
        self.assertIn("- [Wiki](https://en.wikipedia.org/wiki/Python_%28programming_language%29)", footer)
        self.assertEqual(
            markdown_safe_url("https://en.wikipedia.org/wiki/Python_(programming_language)"),
            "https://en.wikipedia.org/wiki/Python_%28programming_language%29",
        )

    def test_footer_plain_and_empty(self):
        footer = render_sources_footer([("https://a.test", None)])
        self.assertEqual(footer, "Sources:\n- https://a.test")
        self.assertEqual(render_sources_footer([]), "")


class TestRetryAfter(unittest.TestCase):
    def test_numeric_date_garbage(self):
        self.assertEqual(parse_retry_after("3"), 3.0)
        future = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertAlmostEqual(parse_retry_after(future), 30.0, delta=5.0)
        self.assertIsNone(parse_retry_after("garbage"))
        self.assertIsNone(parse_retry_after(None))


if __name__ == "__main__":
    unittest.main()
