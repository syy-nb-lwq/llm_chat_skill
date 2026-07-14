"""Provider 管理器 - 动态注册和切换 Provider"""
from typing import Dict, Type, Optional, Any
from dataclasses import dataclass

from infra.providers.base import BaseProvider


@dataclass
class ProviderConfig:
    """Provider 配置"""
    provider_class: Type[BaseProvider]
    config: Dict[str, Any]  # 传递给 Provider 构造函数的参数


class ProviderManager:
    """Provider 动态注册和管理"""

    def __init__(self):
        self._providers: Dict[str, ProviderConfig] = {}
        self._instances: Dict[str, BaseProvider] = {}
        self._current_provider: Optional[str] = None

    def register(
        self,
        name: str,
        provider_class: Type[BaseProvider],
        config: Dict[str, Any],
        set_current: bool = False,
    ) -> None:
        """注册一个 Provider

        Args:
            name: Provider 名称 (如 "openai", "anthropic")
            provider_class: Provider 类
            config: 构造 Provider 的配置参数
            set_current: 是否设为当前 Provider
        """
        self._providers[name] = ProviderConfig(
            provider_class=provider_class,
            config=config,
        )
        if set_current or self._current_provider is None:
            self._current_provider = name

    def get(self, name: Optional[str] = None) -> BaseProvider:
        """获取 Provider 实例(懒加载)"""
        name = name or self._current_provider
        if name is None:
            raise ValueError("没有注册任何 Provider")

        if name not in self._instances:
            if name not in self._providers:
                raise ValueError(f"Provider '{name}' 未注册")
            cfg = self._providers[name]
            self._instances[name] = cfg.provider_class(**cfg.config)

        return self._instances[name]

    def current(self) -> Optional[BaseProvider]:
        """获取当前 Provider"""
        if self._current_provider:
            return self.get(self._current_provider)
        return None

    def switch(self, name: str) -> BaseProvider:
        """切换当前 Provider"""
        if name not in self._providers:
            raise ValueError(f"Provider '{name}' 未注册")
        self._current_provider = name
        # 清除旧实例,下次 get 时重新创建
        if name in self._instances:
            del self._instances[name]
        return self.get(name)

    def list_providers(self) -> list[str]:
        """列出所有已注册的 Provider"""
        return list(self._providers.keys())

    def unregister(self, name: str) -> None:
        """注销 Provider"""
        if name in self._providers:
            del self._providers[name]
        if name in self._instances:
            del self._instances[name]
        if self._current_provider == name:
            # 切换到另一个 Provider
            self._current_provider = next(iter(self._providers), None)

    def reset(self) -> None:
        """重置所有 Provider"""
        self._providers.clear()
        self._instances.clear()
        self._current_provider = None


# 全局单例
_provider_manager: Optional[ProviderManager] = None


def get_provider_manager() -> ProviderManager:
    """获取 ProviderManager 单例"""
    global _provider_manager
    if _provider_manager is None:
        _provider_manager = ProviderManager()
    return _provider_manager


def reset_provider_manager() -> None:
    """重置 ProviderManager(用于测试)"""
    global _provider_manager
    _provider_manager = None
