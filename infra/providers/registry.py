"""Provider 初始化和自动注册"""
from infra.providers.manager import get_provider_manager
from infra.providers.openai import OpenAIProvider
from infra.providers.anthropic import AnthropicProvider
from infra.providers.local import LocalProvider


def init_providers_from_config() -> None:
    """按当前配置注册所有 Provider。

    **为什么要有这个函数**:Provider 注册以前只写在 FastAPI 的 lifespan 里,
    于是所有**独立入口**(nightly 评估脚本、CLI、Streamlit)都会在第一次
    LLM 调用时炸掉:`Provider 'openai' 未注册`。
    那条 `--live` 通道因为需要真实 key 从来没跑过,所以这个坑一直没人踩到。

    统一从配置初始化后,入口只要调一次(或干脆依赖 `LLMClient` 的懒初始化)。
    """
    from infra.config import config

    init_providers(
        openai_api_key=config.openai_api_key,
        openai_base_url=config.openai_base_url,
        openai_model=config.openai_model,
        anthropic_api_key=config.anthropic_api_key,
        anthropic_model=config.anthropic_model,
        local_base_url=config.local_base_url,
        local_model=config.local_model,
        default_provider=config.default_provider,
    )


def init_providers(
    openai_api_key: str = "",
    openai_base_url: str = "https://api.openai.com/v1",
    openai_model: str = "gpt-4o-mini",
    anthropic_api_key: str = "",
    anthropic_model: str = "claude-sonnet-4-20250514",
    local_base_url: str = "http://localhost:11434",
    local_model: str = "llama3.2",
    default_provider: str = "openai",
) -> None:
    """初始化所有 Provider
    
    Args:
        openai_api_key: OpenAI API Key
        openai_base_url: OpenAI API Base URL
        openai_model: OpenAI 默认模型
        anthropic_api_key: Anthropic API Key
        anthropic_model: Anthropic 默认模型
        local_base_url: Ollama Base URL
        local_model: Ollama 默认模型
        default_provider: 默认使用的 Provider 名称
    """
    manager = get_provider_manager()
    
    # 注册 OpenAI Provider
    if openai_api_key:
        manager.register(
            "openai",
            OpenAIProvider,
            config={
                "api_key": openai_api_key,
                "base_url": openai_base_url,
                "default_model": openai_model,
            },
            set_current=(default_provider == "openai"),
        )
    
    # 注册 Anthropic Provider
    if anthropic_api_key:
        manager.register(
            "anthropic",
            AnthropicProvider,
            config={
                "api_key": anthropic_api_key,
                "default_model": anthropic_model,
            },
            set_current=(default_provider == "anthropic"),
        )
    
    # 注册 Local Provider (Ollama)
    manager.register(
        "local",
        LocalProvider,
        config={
            "base_url": local_base_url,
            "default_model": local_model,
        },
        set_current=(default_provider == "local"),
    )
