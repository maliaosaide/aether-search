"""All settings via AETHER_* environment variables (.env supported)."""

import os as _os

# Self-heal: a stale SSL_CERT_FILE (common in some conda envs) breaks httpx/openai SSL context
# creation. Drop it if it points to a non-existent file so the system trust store is used.
_sc = _os.environ.get("SSL_CERT_FILE")
if _sc and not _os.path.exists(_sc):
    _os.environ.pop("SSL_CERT_FILE", None)

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Grok：官方 SDK 处理 Chat Completions 或 Responses，CPA 复用兼容协议。
    grok_api_url: str = Field(default="", description="包含版本前缀的 base URL，例如 https://api.x.ai/v1")
    grok_api_key: str = Field(default="")
    grok_model: str = Field(default="grok-4")
    grok_provider: str = Field(default="auto", description="auto|openai|xai|cpa；auto 仅按 api.x.ai 精确主机名识别 xAI")
    grok_api_format: str = Field(default="auto", description="auto|chat_completions|responses；auto 时 xAI 使用 Responses，其他使用 Chat")
    grok_search_mode: str = Field(default="", description="空值或 auto：xAI 请求原生搜索，其他交由网关；on 显式请求 Responses 搜索工具；off/none 不携带工具")
    grok_endpoints: str = Field(default="", description='JSON 数组，每项 url,key,model?,label?,provider?,api_format?,search_mode?；覆盖单端点配置')
    grok_failover: str = Field(default="sequential", description="sequential 主备切换；race 有限并发竞速，首个完整非空回答胜出")
    grok_timeout: float = Field(default=60.0, description="整个 Grok 调用的总时限，包含所有端点和重试，单位秒")
    grok_request_timeout: float = Field(default=45.0, description="单次请求时限，不超过总预算剩余时间，单位秒")
    grok_max_concurrency: int = Field(default=2, description="race 模式的最大并发端点数")

    # Aux query-agent LLM (any OpenAI-compatible endpoint)
    llm_api_base: str = Field(default="")
    llm_api_key: str = Field(default="")
    llm_model: str = Field(default="")
    llm_rewrite: bool = Field(default=True, description="Rewrite query into sub-queries via LLM")
    llm_timeout: float = Field(default=30.0)

    # Search backends: a configured key auto-enables its connector; unset keys
    # are silently skipped and never affect the rest. Every URL defaults to the
    # official endpoint — override only when routing through a proxy.
    brave_api_key: str = Field(default="")
    brave_api_url: str = Field(default="https://api.search.brave.com/res/v1")
    exa_api_key: str = Field(default="")
    exa_api_url: str = Field(default="https://api.exa.ai")
    serper_api_key: str = Field(default="")
    serper_api_url: str = Field(default="https://google.serper.dev")
    linkup_api_key: str = Field(default="")
    linkup_api_url: str = Field(default="https://api.linkup.so/v1")
    youcom_api_key: str = Field(default="")
    youcom_api_url: str = Field(default="https://api.you.com/v1")
    sonar_api_key: str = Field(default="")
    sonar_api_url: str = Field(default="https://api.perplexity.ai")
    sonar_model: str = Field(default="sonar")
    tavily_api_key: str = Field(default="", description="Leave empty if grok-search already uses your Tavily quota")
    tavily_api_url: str = Field(default="https://api.tavily.com")
    firecrawl_api_key: str = Field(default="", description="Leave empty if grok-search already uses your Firecrawl quota")
    firecrawl_api_url: str = Field(default="https://api.firecrawl.dev/v2")
    parallel_api_key: str = Field(default="")
    parallel_api_url: str = Field(default="https://api.parallel.ai/v1")
    deepseek_api_key: str = Field(default="")
    deepseek_api_url: str = Field(default="https://api.deepseek.com")

    # Fan-out / fusion
    fanout: str = Field(default="economy", description="economy: original x all backends + variants round-robin; full: variants x backends")
    max_variants: int = Field(default=3, description="Sub-query count including the original")
    top_k: int = Field(default=10, description="Results requested per backend call")
    rrf_k: int = Field(default=60)
    max_concurrency: int = Field(default=8)
    connector_timeout: float = Field(default=10.0)
    aux_timeout: float = Field(default=25.0, description="Legacy aux-channel deadline; agent channel uses agent_timeout")

    # Agent channel (second answer channel: LLM tool-calling loop over aux backends)
    agent_max_steps: int = Field(default=6, description="Cap on LLM tool-calling loop steps")
    agent_timeout: float = Field(default=90.0, description="Whole-agent-channel deadline incl. seed search (seconds)")
    fetch_timeout: float = Field(default=60.0, description="Per-service web_fetch timeout (seconds)")
    scrape_retry: int = Field(default=3, description="Firecrawl empty-content retries")
    sources_cache_size: int = Field(default=256, description="LRU cap for get_sources sessions")
    fallback_backends: str = Field(default="", description="补充层后端名（逗号分隔，如 'sonar,linkup'）；不进 search 默认扇出，仅供 search_deep 工具调用")

    # Verification of grok-cited URLs (corroborate against aux + liveness probe)
    verify: bool = Field(default=True)
    verify_timeout: float = Field(default=2.5, description="Per-URL probe timeout")

    retry_max_attempts: int = Field(default=3)
    debug: bool = Field(default=False)

    model_config = SettingsConfigDict(
        env_prefix="AETHER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()


def mask_key(key: str) -> str:
    if not key or len(key) <= 8:
        return "***" if key else "(not set)"
    return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"
