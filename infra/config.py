"""Configuration loaded from .env via pydantic-settings."""
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


class Config(BaseSettings):
    """Application configuration."""

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).parent.parent / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Providers
    default_provider: str = Field("openai", description="Default LLM provider")
    openai_api_key: str = Field("", description="OpenAI API key")
    openai_base_url: str = Field("https://api.openai.com/v1")
    openai_model: str = Field("gpt-4o-mini")
    anthropic_api_key: str = Field("", description="Anthropic API key")
    anthropic_model: str = Field("claude-sonnet-4-20250514")
    local_base_url: str = Field("http://localhost:11434")
    local_model: str = Field("llama3.2")

    # Runtime
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    request_timeout_s: int = Field(60, ge=1)
    skills_path: Path = Field("skills")
    memory_path: Path = Field("memory")
    vector_path: Path = Field("vector_store")
    max_iterations: int = Field(10, ge=1, le=100)
    session_ttl_s: int = Field(3600, ge=60)

    # Logging
    log_level: str = Field("INFO")
    log_to_file: bool = Field(False)
    log_dir: Path = Field("logs")

    # Feature flags
    skill_dag_enabled: bool = Field(False)
    tool_cache_enabled: bool = Field(False)
    self_evolution_enabled: bool = Field(False)
    multi_provider_enabled: bool = Field(False, description="Enable multiple providers")
    semantic_memory_enabled: bool = Field(False, description="Enable semantic memory")
    soul_enabled: bool = Field(False, description="Enable soul system")

    # Embeddings
    embedding_provider: str = Field("mock", description="Embedding provider")
    embedding_api_key: str = Field("", description="Embedding API key")
    embedding_base_url: str = Field("https://api.openai.com/v1")
    embedding_model: str = Field("text-embedding-3-small")
    embedding_dimension: int = Field(1536)

    # Soul / MCP / intent
    soul_path: Path = Field("soul/SOUL.md")
    mcp_enabled: bool = Field(False, description="Enable MCP tools")
    mcp_servers: str = Field("", description="MCP servers JSON array")
    intent_mode: str = Field("rule_first", description="Intent detection mode")
    intent_rule_threshold: int = Field(5, description="Rule threshold")
    intent_llm_fallback: bool = Field(True, description="Fallback to LLM")

    def validate(self) -> None:
        """Validate startup configuration."""
        if self.multi_provider_enabled:
            if not self.openai_api_key and not self.anthropic_api_key:
                raise ConfigError(
                    "Multi-provider mode requires OPENAI_API_KEY or ANTHROPIC_API_KEY."
                )
            return

        if not self.openai_api_key:
            raise ConfigError(
                "OPENAI_API_KEY is not configured. Set it in .env or copy from .env.example."
            )

    def set_feature_flag(self, name: str, value: bool, *, persist: bool = True) -> bool:
        """Update a boolean feature flag and optionally persist it to .env."""
        if not hasattr(self, name):
            raise ConfigError(f"Unknown config field: {name}")

        current = getattr(self, name)
        if not isinstance(current, bool):
            raise ConfigError(f"Config field is not boolean: {name}")

        setattr(self, name, bool(value))
        if persist:
            self._persist_env_var(name.upper(), "true" if value else "false")
        return getattr(self, name)

    def _persist_env_var(self, key: str, value: str) -> None:
        env_path = Path(self.model_config.get("env_file") or ".env")
        lines = []
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()

        new_line = f"{key}={value}"
        replaced = False
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.split("=", 1)[0].strip().upper() == key:
                lines[idx] = new_line
                replaced = True
                break

        if not replaced:
            lines.append(new_line)

        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


config = Config()
