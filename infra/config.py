"""配置 - 使用 pydantic-settings 加载"""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class ConfigError(RuntimeError):
    """配置错误"""


class Config(BaseSettings):
    """全局配置,从 .env 文件加载"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----- LLM -----
    llm_api_key: str = Field("", description="LLM API Key")
    llm_base_url: str = Field("https://api.openai.com/v1")
    llm_model: str = Field("gpt-4o-mini")
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

    def validate(self) -> None:
        """启动时调用,失败抛 ConfigError"""
        if not self.llm_api_key:
            raise ConfigError(
                "LLM_API_KEY 未设置,请在 .env 文件中配置(参考 .env.example)"
            )


# 全局单例
config = Config()