"""Grok 通道:基于官方 openai SDK 的精简实现,不导入 channels/sources/verify/agent。

- 传输、SSE 分帧、增量 UTF-8、连接管理全部交给 ``AsyncOpenAI``(``max_retries=0``,
  重试策略由本通道统一掌握);
- 三种接入形态(openai 兼容 / xAI 官方 / CPA 自建)共享同一 Chat 与 Responses 路径;
- sequential 默认;race 用信号量限并发 + as_completed 竞争,首个完整成功获胜;
- 共享额外重试计数:整个调用最多 端点数 + retry_max_attempts 次请求;
  仅瞬时错误(网络/408/429/500/502/503/504/流内 server_error)重试,
  鉴权/参数错误不重试;
- 构造函数永不因配置异常抛出;失败时 ``text`` 恒为空串、``evidence`` 恒为 ``[]``。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field

import httpx
import openai
from openai import AsyncOpenAI

from aether_search.channel_types import AnswerResult
from aether_search.grok_protocol import (
    API_FORMATS,
    CHAT_FINISH_FAILURES,
    CHAT_FINISH_UNSUPPORTED,
    CHAT_STREAM_NO_STOP,
    PROVIDERS,
    RESPONSES_ERROR_EVENT,
    RESPONSES_NO_COMPLETION,
    RESPONSES_STATUS_FAILURES,
    RESPONSES_STATUS_UNSUPPORTED,
    SEARCH_MODES,
    CitationCollector,
    ProtocolError,
    build_system_prompt,
    build_user_prompt,
    initial_search_state,
    markdown_safe_url,
    parse_retry_after,
    render_sources_footer,
    resolve_api_format,
    resolve_provider,
    resolve_tools,
    normalize_base_url,
)

CHANNEL_NAME = "grok"
FAILOVERS = frozenset({"sequential", "race"})
TRANSIENT_HTTP = frozenset({408, 429, 500, 502, 503, 504})
# 网关以 SSE event:error 返回的瞬时服务端错误(SDK 抛 APIError,无 HTTP 状态码)
TRANSIENT_STREAM_TYPES = frozenset({"server_error", "rate_limit_error"})
TRANSIENT_STREAM_CODES = frozenset({
    "internal_server_error", "server_error", "service_unavailable", "rate_limit_exceeded",
})
RETRY_BACKOFF_BASE = 0.4
RETRY_BACKOFF_MAX = 4.0


def _is_transient_stream_error(exc: openai.APIError) -> bool:
    body = getattr(exc, "body", None)
    return isinstance(body, dict) and (
        body.get("type") in TRANSIENT_STREAM_TYPES or body.get("code") in TRANSIENT_STREAM_CODES
    )


def _is_num(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


@dataclass
class GrokOptions:
    """与集成后 config 字段一一对应;key/endpoints 不进入 repr。"""

    api_url: str = ""
    api_key: str = ""
    model: str = ""
    endpoints: str | list | None = ""
    provider: str = "auto"
    api_format: str = "auto"
    search_mode: str = ""
    failover: str = "sequential"
    timeout: float = 60.0
    request_timeout: float = 45.0
    max_concurrency: int = 2
    retry_max_attempts: int = 3

    def __repr__(self) -> str:  # 密钥绝不进入 repr
        return (
            "GrokOptions(api_url=<str>, api_key=<redacted>, model=<str>, endpoints=<redacted>, "
            "provider=%r, api_format=%r, search_mode=%r, failover=%r, timeout=%r, "
            "request_timeout=%r, max_concurrency=%r, retry_max_attempts=%r)"
            % (
                self.provider, self.api_format, self.search_mode, self.failover,
                self.timeout, self.request_timeout, self.max_concurrency,
                self.retry_max_attempts,
            )
        )

    @classmethod
    def from_settings(cls, settings: object) -> "GrokOptions":
        """读取公共 config 值:缺失用默认;显式存在的非法类型原样透传,由校验报脱敏错误。"""
        missing = object()

        def raw(name: str, default: object) -> object:
            try:
                value = getattr(settings, name, missing)
            except Exception:
                return default
            return default if value is missing else value

        def passthrough(value: object, strip: bool = True) -> object:
            return value.strip() if isinstance(value, str) and strip else value

        endpoints = raw("grok_endpoints", "")
        if endpoints is None:
            endpoints = ""
        return cls(
            api_url=passthrough(raw("grok_api_url", "")),
            api_key=passthrough(raw("grok_api_key", "")),
            model=passthrough(raw("grok_model", ""), strip=False),  # 模型名原样保留
            endpoints=endpoints,
            provider=passthrough(raw("grok_provider", "auto")) or "auto",
            api_format=passthrough(raw("grok_api_format", "auto")) or "auto",
            search_mode=passthrough(raw("grok_search_mode", "")),
            failover=passthrough(raw("grok_failover", "sequential")) or "sequential",
            timeout=raw("grok_timeout", 60.0),
            request_timeout=raw("grok_request_timeout", 45.0),
            max_concurrency=raw("grok_max_concurrency", 2),
            retry_max_attempts=raw("retry_max_attempts", 3),
        )


@dataclass
class GrokEndpoint:
    url: str
    key: str
    model: str
    label: str
    provider: str
    api_format: str
    search_mode: str

    def __repr__(self) -> str:  # 密钥绝不进入 repr
        return (
            "GrokEndpoint(url=<str>, key=<redacted>, model=<str>, label=%r, provider=%r, "
            "api_format=%r, search_mode=%r)"
            % (self.label, self.provider, self.api_format, self.search_mode)
        )


def _endpoint_from_item(item: dict, index: int, options: GrokOptions, errors: list[str]):
    def field_str(name: str, default: str, *, empty_inherits: bool = True) -> str | None:
        """显式存在的字段必须是字符串;错误类型报脱敏错误而非静默继承。"""
        if name not in item:
            return default
        value = item[name]
        if not isinstance(value, str):
            errors.append(f"config: endpoint {name} must be a string")
            return None
        if value == "" and empty_inherits:
            return default
        return value

    url = field_str("url", "", empty_inherits=False) or ""
    if not url:
        errors.append("config: endpoint url is missing or empty")
        return None
    key = field_str("key", _as_str(options.api_key))
    if not key:
        errors.append("config: endpoint key is missing")
        return None
    model = field_str("model", _as_str(options.model), empty_inherits=False)
    if not (model or "").strip():
        errors.append("config: endpoint model is missing")
        return None
    label = field_str("label", f"endpoint-{index + 1}")
    if label is None:
        return None
    if len(label) > 64 or any(ord(ch) < 32 or ord(ch) == 127 for ch in label):
        errors.append("config: endpoint label must be printable and at most 64 chars")
        return None
    provider_cfg = field_str("provider", _as_str(options.provider) or "auto")
    format_cfg = field_str("api_format", _as_str(options.api_format) or "auto")
    # search_mode 的空串是合法覆盖值(覆盖全局 on),不做空串继承
    if "search_mode" in item and not isinstance(item["search_mode"], str):
        errors.append("config: endpoint search_mode must be a string")
        return None
    search_cfg = item["search_mode"] if "search_mode" in item else _as_str(options.search_mode)
    if provider_cfg is None or format_cfg is None:
        return None
    if provider_cfg not in PROVIDERS:
        errors.append("config: unsupported grok_provider value (expected auto|openai|xai|cpa)")
        return None
    if format_cfg not in API_FORMATS:
        errors.append("config: unsupported grok_api_format value (expected auto|chat_completions|responses)")
        return None
    if search_cfg not in SEARCH_MODES:
        errors.append("config: unsupported grok_search_mode value (expected ''|auto|on|off|none)")
        return None
    provider = resolve_provider(url, provider_cfg)
    api_format = resolve_api_format(provider, format_cfg)
    if provider == "xai" and api_format == "chat_completions":
        errors.append("config: xai provider does not support the chat_completions format")
        return None
    try:
        normalize_base_url(url)
        resolve_tools(api_format, search_cfg, provider)
    except ProtocolError as exc:
        errors.append(f"config: {exc}")
        return None
    return GrokEndpoint(url, key, model, label, provider, api_format, search_cfg)


def resolve_endpoints(options: GrokOptions) -> tuple[list[GrokEndpoint], list[str]]:
    """解析校验端点;有错则返回 ([], 脱敏错误列表)。"""
    errors: list[str] = []
    for name, value in (
        ("grok_api_url", options.api_url), ("grok_api_key", options.api_key),
        ("grok_model", options.model), ("grok_provider", options.provider),
        ("grok_api_format", options.api_format), ("grok_search_mode", options.search_mode),
        ("grok_failover", options.failover),
    ):
        if value is not None and not isinstance(value, str):
            errors.append(f"config: {name} must be a string")
    if options.provider not in PROVIDERS:
        errors.append("config: unsupported grok_provider value (expected auto|openai|xai|cpa)")
    if options.api_format not in API_FORMATS:
        errors.append("config: unsupported grok_api_format value (expected auto|chat_completions|responses)")
    if options.search_mode not in SEARCH_MODES:
        errors.append("config: unsupported grok_search_mode value (expected ''|auto|on|off|none)")
    if options.failover not in FAILOVERS:
        errors.append("config: unsupported grok_failover value (expected sequential|race)")
    for name, value in (("grok_timeout", options.timeout), ("grok_request_timeout", options.request_timeout)):
        if not _is_num(value) or not (float(value) > 0) or value != value or value in (float("inf"), float("-inf")):
            errors.append(f"config: {name} must be a positive finite number")
    if not _is_int(options.max_concurrency) or options.max_concurrency < 1:
        errors.append("config: grok_max_concurrency must be an integer >= 1")
    if not _is_int(options.retry_max_attempts) or options.retry_max_attempts < 0:
        errors.append("config: retry_max_attempts must be an integer >= 0")
    if errors:
        return [], errors

    raw_items: list[dict] = []
    if options.endpoints:
        parsed = options.endpoints
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except ValueError:
                return [], ["config: grok_endpoints must be a JSON array of objects"]
        if not isinstance(parsed, list) or not parsed:
            return [], ["config: grok_endpoints must be a non-empty JSON array"]
        for entry in parsed:
            if not isinstance(entry, dict):
                return [], ["config: grok_endpoints entries must be JSON objects"]
            raw_items.append(entry)
    elif _as_str(options.api_url):
        raw_items.append({"url": options.api_url})

    endpoints: list[GrokEndpoint] = []
    for i, item in enumerate(raw_items):
        endpoint = _endpoint_from_item(item, i, options, errors)
        if endpoint is not None:
            endpoints.append(endpoint)
    if errors:
        return [], errors
    return endpoints, []


# ---------------------------------------------------------------------------
# 通道
# ---------------------------------------------------------------------------


@dataclass
class _Attempt:
    ok: bool = False
    retryable: bool = False
    wait_hint: float | None = None
    error: str | None = None
    text: str = ""
    citations: list[tuple[str, str | None]] = field(default_factory=list)
    search_state: str = "not_requested"


@dataclass
class _Outcome:
    error: str | None
    text: str = ""
    citations: list[tuple[str, str | None]] = field(default_factory=list)
    search_state: str = "not_requested"
    provider: str = ""
    api_format: str = ""
    label: str = ""
    attempts: int = 0


class _Run:
    """一次 answer 调用的共享状态:总期限、请求计数与共享额外重试额度。"""

    def __init__(self, options: GrokOptions) -> None:
        self.deadline = time.monotonic() + float(options.timeout)
        self.request_timeout = float(options.request_timeout)
        self.attempts = 0
        self.extra_retries = int(options.retry_max_attempts)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def take_attempt(self) -> None:
        self.attempts += 1


class GrokChannel:
    """Grok 检索通道;构造永不因配置问题抛异常。"""

    def __init__(
        self,
        options: GrokOptions | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        trust_env: bool = True,
    ) -> None:
        self._transport = transport
        self._trust_env = trust_env
        self._options = options
        self._resolved: tuple[list[GrokEndpoint], list[str]] | None = None
        self._config_error: str | None = None

    def _ensure(self) -> tuple[list[GrokEndpoint], list[str]]:
        if self._resolved is not None:
            return self._resolved
        if self._options is None and self._config_error is None:
            try:
                from aether_search import config  # 惰性读取,模块导入期零依赖

                self._options = GrokOptions.from_settings(getattr(config, "settings", None))
            except Exception:
                self._config_error = "config: settings unavailable"
                self._options = GrokOptions()
        try:
            assert self._options is not None
            self._resolved = resolve_endpoints(self._options)
        except Exception:  # 任何配置异常都不得击穿外层 Agent 编排
            self._resolved = ([], ["config: invalid grok options"])
        return self._resolved

    @property
    def options(self) -> GrokOptions:
        self._ensure()
        assert self._options is not None
        return self._options

    @property
    def endpoints(self) -> list[GrokEndpoint]:
        return list(self._ensure()[0])  # 无效配置时安全暴露空列表

    def enabled(self) -> bool:
        """显式 endpoints(即使格式非法)→ True;否则需同时配置 url 与 key。"""
        self._ensure()
        if self._config_error is not None:
            return False
        opts = self._options
        assert opts is not None
        if opts.endpoints:
            return True
        return bool(_as_str(opts.api_url)) and bool(_as_str(opts.api_key))

    async def answer(self, query: str, platform: str = "") -> AnswerResult:
        start = time.perf_counter()
        endpoints, errors = self._ensure()

        def fail(error: str, detail: str = "") -> AnswerResult:
            return AnswerResult(
                CHANNEL_NAME, "", [], [], detail or error,
                round(time.perf_counter() - start, 4), error,
            )

        if self._config_error is not None:
            return fail(self._config_error)
        if errors:
            return fail(errors[0], "; ".join(errors))
        if not endpoints:
            return fail("grok channel not configured")

        options = self._options
        assert options is not None
        run = _Run(options)
        system_prompt = build_system_prompt()
        user_prompt = build_user_prompt(query, platform)
        clients: list[tuple[GrokEndpoint, AsyncOpenAI]] = []
        try:
            for endpoint in endpoints:
                clients.append((endpoint, self._client(endpoint, options)))
            try:
                async with asyncio.timeout(float(options.timeout)):
                    if options.failover == "race":
                        outcome = await self._race(run, clients, system_prompt, user_prompt)
                    else:
                        outcome = await self._sequential(run, clients, system_prompt, user_prompt)
            except TimeoutError:
                outcome = _Outcome(error="timeout: total budget exhausted", attempts=run.attempts)
            except asyncio.CancelledError:
                raise  # 调用方取消必须向上传播
        except Exception:
            return fail("grok client error")
        finally:
            await asyncio.gather(*(client.close() for _endpoint, client in clients), return_exceptions=True)

        elapsed = round(time.perf_counter() - start, 4)
        if outcome.error is not None:
            detail = (
                f"provider={outcome.provider}; format={outcome.api_format}; "
                f"endpoint={outcome.label}; attempts={outcome.attempts or run.attempts}"
                if outcome.label
                else f"attempts={outcome.attempts or run.attempts}"
            )
            return AnswerResult(CHANNEL_NAME, "", [], [], detail, elapsed, outcome.error)

        footer_items = outcome.citations
        text = outcome.text + ("\n\n" + render_sources_footer(footer_items) if footer_items else "")
        detail = (
            f"provider={outcome.provider}; format={outcome.api_format}; "
            f"endpoint={outcome.label}; attempts={outcome.attempts}; search={outcome.search_state}"
        )
        return AnswerResult(
            CHANNEL_NAME, text,
            [markdown_safe_url(url) for url, _title in footer_items],
            [], detail, elapsed, None,
        )

    # -- 客户端与单次尝试 --------------------------------------------------

    def _client(self, endpoint: GrokEndpoint, options: GrokOptions) -> AsyncOpenAI:
        return AsyncOpenAI(
            base_url=normalize_base_url(endpoint.url),
            api_key=endpoint.key,
            max_retries=0,  # 重试策略由本通道统一掌握
            http_client=httpx.AsyncClient(
                transport=self._transport,
                trust_env=self._trust_env,
                timeout=httpx.Timeout(float(options.timeout) * 2),
                follow_redirects=False,
            ),
        )

    async def _attempt(self, endpoint: GrokEndpoint, client: AsyncOpenAI,
                       system_prompt: str, user_prompt: str) -> _Attempt:
        search_state = initial_search_state(endpoint.api_format, endpoint.search_mode, endpoint.provider)
        if endpoint.api_format == "responses":
            return await self._attempt_responses(endpoint, client, system_prompt, user_prompt, search_state)
        return await self._attempt_chat(endpoint, client, system_prompt, user_prompt, search_state)

    async def _attempt_chat(self, endpoint, client, system_prompt, user_prompt, search_state) -> _Attempt:
        collector = CitationCollector()
        texts: list[str] = []
        finish: str | None = None
        try:
            stream = await client.chat.completions.create(
                model=endpoint.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                stream=True,
            )
            async with stream:
                async for chunk in stream:
                    collector.add_citations(getattr(chunk, "citations", None))
                    for choice in getattr(chunk, "choices", None) or []:
                        if getattr(choice, "index", 0) != 0:
                            continue  # 多候选是备选答案,不是内容分段
                        delta = getattr(choice, "delta", None)
                        if delta is not None:
                            content = getattr(delta, "content", None)
                            if isinstance(content, str) and content:
                                texts.append(content)
                            for annotation in getattr(delta, "annotations", None) or []:
                                collector.add_annotation(annotation)
                        if getattr(choice, "finish_reason", None) is not None:
                            finish = choice.finish_reason
        except openai.APIStatusError as exc:
            return self._status_attempt(exc, search_state)
        except openai.APIConnectionError:  # 含超时:可重试;不外泄异常文本
            return _Attempt(retryable=True, error="network error", search_state=search_state)
        except openai.APIError as exc:
            return _Attempt(retryable=_is_transient_stream_error(exc),
                            error="chat stream error", search_state=search_state)

        if finish is not None and finish != "stop":
            # 已知截断/过滤/工具调用 + 未知取值一律失败;错误消息为固定常量
            return _Attempt(
                error=CHAT_FINISH_FAILURES.get(finish, CHAT_FINISH_UNSUPPORTED),
                search_state=search_state,
            )
        if finish != "stop":
            return _Attempt(error=CHAT_STREAM_NO_STOP, search_state=search_state)
        text = "".join(texts)
        if not text.strip():
            return _Attempt(error="empty answer text", search_state=search_state)
        collector.add_text_links(text)
        return _Attempt(ok=True, text=text, citations=collector.items(), search_state=search_state)

    async def _attempt_responses(self, endpoint, client, system_prompt, user_prompt, search_state) -> _Attempt:
        collector = CitationCollector()
        web_observed = False
        completed_text: str | None = None
        error: str | None = None
        kwargs: dict = {
            "model": endpoint.model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
            ],
            "stream": True,
            "store": False,
        }
        tools = resolve_tools(endpoint.api_format, endpoint.search_mode, endpoint.provider)
        if tools:
            kwargs["tools"] = tools
        try:
            stream = await client.responses.create(**kwargs)
            async with stream:
                async for event in stream:
                    kind = getattr(event, "type", "")
                    if kind == "response.completed":
                        completed_text, observed, error = _harvest_completed(
                            getattr(event, "response", None), collector
                        )
                        web_observed = web_observed or observed
                        break  # 终止事件,立即停止消费
                    if kind == "response.failed":
                        error = "responses failed"
                        break
                    if kind == "response.incomplete":
                        error = "responses incomplete"
                        break
                    if kind == "error":
                        error = RESPONSES_ERROR_EVENT
                        break
                    if kind == "response.web_search_call.completed":
                        web_observed = True
                    elif kind == "response.output_text.annotation.added":
                        collector.add_annotation(getattr(event, "annotation", None))
                    # 其余已知/未知事件安全忽略,不猜测为完成
        except openai.APIStatusError as exc:
            return self._status_attempt(exc, search_state)
        except openai.APIConnectionError:
            return _Attempt(retryable=True, error="network error", search_state=search_state)
        except openai.APIError as exc:
            return _Attempt(retryable=_is_transient_stream_error(exc),
                            error="responses stream error", search_state=search_state)

        if error is not None:
            return _Attempt(error=error, search_state=search_state)
        if completed_text is None:
            return _Attempt(error=RESPONSES_NO_COMPLETION, search_state=search_state)
        text = completed_text
        if not text.strip():
            return _Attempt(error="empty answer text", search_state=search_state)
        state = "observed" if web_observed else search_state
        return _Attempt(ok=True, text=text, citations=collector.items(), search_state=state)

    @staticmethod
    def _status_attempt(exc: openai.APIStatusError, search_state: str) -> _Attempt:
        status = exc.status_code
        retryable = status in TRANSIENT_HTTP
        hint = (
            parse_retry_after(exc.response.headers.get("retry-after"))
            if retryable and exc.response is not None else None
        )
        return _Attempt(
            retryable=retryable, wait_hint=hint,
            error=f"HTTP {status}", search_state=search_state,
        )

    # -- 调度 --------------------------------------------------------------

    async def _try_endpoint(self, run: _Run, endpoint: GrokEndpoint, client: AsyncOpenAI,
                            system_prompt: str, user_prompt: str) -> _Outcome:
        """单端点:初始尝试 + 合格重试(共享额外重试额度)。"""
        backoff_index = 0
        while True:
            if run.remaining() <= 0:
                return _Outcome(error="timeout: total budget exhausted", attempts=run.attempts)
            run.take_attempt()
            try:
                async with asyncio.timeout(min(run.request_timeout, run.remaining())):
                    attempt = await self._attempt(endpoint, client, system_prompt, user_prompt)
            except TimeoutError:
                attempt = _Attempt(retryable=True, error="timeout: request exceeded budget")
            except asyncio.CancelledError:
                raise
            except Exception:
                attempt = _Attempt(error="invalid endpoint response")
            if attempt.ok:
                return _Outcome(
                    error=None, text=attempt.text, citations=attempt.citations,
                    search_state=attempt.search_state, provider=endpoint.provider,
                    api_format=endpoint.api_format, label=endpoint.label,
                    attempts=run.attempts,
                )
            last = attempt.error or "endpoint failed"
            if not attempt.retryable or run.extra_retries <= 0:
                return _Outcome(error=last, provider=endpoint.provider,
                                api_format=endpoint.api_format, label=endpoint.label,
                                attempts=run.attempts)
            wait = attempt.wait_hint
            if wait is None:
                wait = min(RETRY_BACKOFF_BASE * (2 ** backoff_index), RETRY_BACKOFF_MAX) \
                    * random.uniform(0.8, 1.2)
                backoff_index += 1
            if wait >= run.remaining():
                return _Outcome(error=last, provider=endpoint.provider,
                                api_format=endpoint.api_format, label=endpoint.label,
                                attempts=run.attempts)
            run.extra_retries -= 1
            await asyncio.sleep(wait)

    async def _sequential(self, run: _Run, clients, system_prompt: str, user_prompt: str) -> _Outcome:
        last = _Outcome(error="no endpoint attempted", attempts=0)
        for endpoint, client in clients:
            outcome = await self._try_endpoint(run, endpoint, client, system_prompt, user_prompt)
            if outcome.error is None:
                return outcome
            last = outcome
            if run.remaining() <= 0:
                break
        return last

    async def _race(self, run: _Run, clients, system_prompt: str, user_prompt: str) -> _Outcome:
        options = self._options
        assert options is not None
        semaphore = asyncio.Semaphore(max(1, int(options.max_concurrency)))
        last = _Outcome(error="race produced no successful endpoint", attempts=0)

        async def one(endpoint: GrokEndpoint, client: AsyncOpenAI) -> _Outcome:
            async with semaphore:
                return await self._try_endpoint(run, endpoint, client, system_prompt, user_prompt)

        tasks = [asyncio.create_task(one(ep, cl)) for ep, cl in clients]
        try:
            for awaiting in asyncio.as_completed(tasks):
                outcome = await awaiting
                if outcome.error is None:
                    return outcome  # 首个完整非空成功结果获胜
                last = outcome
            return last
        finally:
            for task in tasks:  # 成功/超时/调用方取消:取消并等待所有输家
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def _harvest_completed(response: object, collector) -> tuple[str | None, bool, str | None]:
    """从 response.completed 的权威响应对象提取文本/引用/搜索观测。

    仅 assistant 消息里的 output_text 是最终答案;reasoning/tool/user 内容绝不采用。
    返回 (text, web_search_observed, error);status 未完成时 text 为 None。
    """
    if response is None:
        return None, False, "responses completed without response object"
    status = getattr(response, "status", None)
    if status is not None and status != "completed":
        return None, False, RESPONSES_STATUS_FAILURES.get(status, RESPONSES_STATUS_UNSUPPORTED)
    texts: dict[int, dict[int, str]] = {}
    observed = False
    for oi, item in enumerate(getattr(response, "output", None) or []):
        if getattr(item, "type", None) == "web_search_call" and getattr(item, "status", None) == "completed":
            observed = True
        if getattr(item, "type", None) != "message":
            continue
        if getattr(item, "role", None) not in (None, "assistant"):
            continue
        for ci, part in enumerate(getattr(item, "content", None) or []):
            if getattr(part, "type", None) != "output_text":
                continue
            text = getattr(part, "text", None)
            if isinstance(text, str) and text:
                texts.setdefault(oi, {})[ci] = text
            for annotation in getattr(part, "annotations", None) or []:
                collector.add_annotation(annotation)
    joined = "\n\n".join(
        "".join(parts[ci] for ci in sorted(parts)) for _oi, parts in sorted(texts.items())
    )
    collector.add_citations(getattr(response, "citations", None))
    collector.add_text_links(joined)
    return joined, observed, None
