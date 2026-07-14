"""配置 - 使用 pydantic-settings 加载"""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class ConfigError(RuntimeError):
    """配置错误"""


class Config(BaseSettings):
    """全局配置,从 .env 文件加载"""

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).parent.parent / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----- LLM Provider -----
    # 默认 Provider
    default_provider: str = Field("openai", description="默认 LLM Provider: openai/anthropic/local")
    
    # OpenAI 配置
    openai_api_key: str = Field("", description="OpenAI API Key")
    openai_base_url: str = Field("https://api.openai.com/v1")
    openai_model: str = Field("gpt-4o-mini")
    
    # Anthropic (Claude) 配置
    anthropic_api_key: str = Field("", description="Anthropic API Key")
    anthropic_model: str = Field("claude-sonnet-4-20250514")
    
    # Local (Ollama) 配置
    local_base_url: str = Field("http://localhost:11434")
    local_model: str = Field("llama3.2")
    
    # 通用配置
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    request_timeout_s: int = Field(60, ge=1)

    # ----- 路径 -----
    skills_path: Path = Field("skills")
    memory_path: Path = Field("memory")
    vector_path: Path = Field("vector_store")

    # ----- 运行 -----
    max_iterations: int = Field(10, ge=1, le=100)
    session_ttl_s: int = Field(3600, ge=60)

    # ----- 日志 -----
    log_level: str = Field("INFO")
    log_to_file: bool = Field(False)
    log_dir: Path = Field("logs")

    # ----- Feature Flags -----
    skill_dag_enabled: bool = Field(False)
    tool_cache_enabled: bool = Field(False)
    self_evolution_enabled: bool = Field(False)
    multi_provider_enabled: bool = Field(False, description="启用多 Provider 支持")
    semantic_memory_enabled: bool = Field(False, description="启用语义记忆(需要 embedding 服务)")
    soul_enabled: bool = Field(False, description="启用 Soul 身份系统")

    # ----- Embedding 配置 -----
    embedding_provider: str = Field("mock", description="embedding 提供商: openai/local/mock")
    embedding_api_key: str = Field("", description="Embedding API Key")
    embedding_base_url: str = Field("https://api.openai.com/v1")
    embedding_model: str = Field("text-embedding-3-small")
    embedding_dimension: int = Field(1536)

    # ----- Soul 配置 -----
    soul_path: Path = Field("soul/SOUL.md")

    # ----- MCP 配置 -----
    mcp_enabled: bool = Field(False, description="启用 MCP 工具协议")
    mcp_servers: str = Field("", description="MCP 服务器配置(JSON数组)")

    # ----- 意图识别配置 -----
    intent_mode: str = Field("rule_first", description="意图识别模式: rule_first/llm_always")
    intent_rule_threshold: int = Field(5, description="规则匹配阈值(字符数)")
    intent_llm_fallback: bool = Field(True, description="规则无法判断时是否调用 LLM")

    def validate(self) -> None:
        """启动时调用,失败抛 ConfigError"""
        if self.multi_provider_enabled:
            # 多 Provider 模式下,至少要有一个 Provider 配置
            if not self.openai_api_key and not self.anthropic_api_key:
                raise ConfigError(
                    "多 Provider 模式需要配置至少一个 API Key "
                    "(OPENAI_API_KEY 或 ANTHROPIC_API_KEY)"
                )
        else:
            # 默认使用 OpenAI
            if not self.openai_api_key:
                raise ConfigError(
                    "OPENAI_API_KEY 未设置,请在 .env 文件中配置(参考 .env.example)"
                )


# 全局单例
config = Config()
