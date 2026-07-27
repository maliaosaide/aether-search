"""All settings via AETHER_* environment variables (.env supported)."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Grok channel (primary). Any OpenAI-compatible endpoint: https://api.x.ai/v1,
    # https://openrouter.ai/api/v1, or a self-hosted proxy. Just URL + key.
    grok_api_url: str = Field(default="", description="OpenAI-compatible endpoint, e.g. https://api.x.ai/v1")
    grok_api_key: str = Field(default="")
    grok_model: str = Field(default="grok-4")
    grok_search_mode: str = Field(default="", description="xAI Live Search: auto|on|off|none. Empty = auto on api.x.ai, omitted elsewhere (strict gateways may reject the extra field)")
    grok_timeout: float = Field(default=60.0, description="Whole-channel deadline in seconds")

    # Aux query-agent LLM (any OpenAI-compatible endpoint)
    llm_api_base: str = Field(default="")
    llm_api_key: str = Field(default="")
    llm_model: str = Field(default="")
    llm_rewrite: bool = Field(default=True, description="Rewrite query into sub-queries via LLM")
    llm_digest: bool = Field(default=False, description="Compress fused aux results into a cited digest (extra tokens)")
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
    youcom_api_url: str = Field(default="https://api.ydc-index.io/v1")
    sonar_api_key: str = Field(default="")
    sonar_api_url: str = Field(default="https://api.perplexity.ai")
    sonar_model: str = Field(default="sonar")
    tavily_api_key: str = Field(default="", description="Leave empty if grok-search already uses your Tavily quota")
    tavily_api_url: str = Field(default="https://api.tavily.com")
    firecrawl_api_key: str = Field(default="", description="Leave empty if grok-search already uses your Firecrawl quota")
    firecrawl_api_url: str = Field(default="https://api.firecrawl.dev/v2")
    parallel_api_key: str = Field(default="")
    parallel_api_url: str = Field(default="https://api.parallel.ai/v1")

    # Fan-out / fusion
    fanout: str = Field(default="economy", description="economy: original x all backends + variants round-robin; full: variants x backends")
    max_variants: int = Field(default=3, description="Sub-query count including the original")
    top_k: int = Field(default=10, description="Results requested per backend call")
    rrf_k: int = Field(default=60)
    max_concurrency: int = Field(default=8)
    connector_timeout: float = Field(default=10.0)
    aux_timeout: float = Field(default=25.0, description="Whole aux-channel deadline in seconds")

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
