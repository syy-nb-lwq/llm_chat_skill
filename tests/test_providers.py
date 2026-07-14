"""Provider 系统单元测试"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from infra.providers.base import BaseProvider, ChatDelta, ChatMessage
from infra.providers.manager import ProviderManager, get_provider_manager, reset_provider_manager
from infra.providers.openai import OpenAIProvider
from infra.providers.anthropic import AnthropicProvider
from infra.providers.local import LocalProvider


class MockProvider(BaseProvider):
    """测试用 Mock Provider"""
    name = "mock"
    
    async def chat(self, messages, **kwargs) -> str:
        return "mock response"
    
    async def chat_stream(self, messages, **kwargs):
        for word in ["mock", "response"]:
            yield ChatDelta(content=word)


class TestProviderManager:
    """ProviderManager 测试"""
    
    def setup_method(self):
        """每个测试前重置单例"""
        reset_provider_manager()
    
    def test_register_provider(self):
        """测试注册 Provider"""
        manager = get_provider_manager()
        
        manager.register(
            "test",
            MockProvider,
            config={},
            set_current=True,
        )
        
        assert "test" in manager.list_providers()
        assert manager.current() is not None
        assert manager.current().name == "mock"
    
    def test_register_multiple_providers(self):
        """测试注册多个 Provider"""
        manager = get_provider_manager()
        
        manager.register("provider1", MockProvider, config={})
        manager.register("provider2", MockProvider, config={})
        
        assert len(manager.list_providers()) == 2
    
    def test_switch_provider(self):
        """测试切换 Provider"""
        manager = get_provider_manager()
        
        manager.register("p1", MockProvider, config={}, set_current=True)
        manager.register("p2", MockProvider, config={})
        
        p2 = manager.switch("p2")
        assert p2.name == "mock"
        assert manager._current_provider == "p2"
    
    def test_switch_nonexistent_provider(self):
        """测试切换不存在的 Provider"""
        manager = get_provider_manager()
        
        with pytest.raises(ValueError, match="未注册"):
            manager.switch("nonexistent")
    
    def test_get_lazy_loading(self):
        """测试懒加载"""
        manager = get_provider_manager()
        
        manager.register("lazy", MockProvider, config={})
        
        # 获取前不应有实例
        assert "lazy" not in manager._instances
        
        # 获取后应该有实例
        instance = manager.get("lazy")
        assert instance is not None
        assert "lazy" in manager._instances
    
    def test_unregister_provider(self):
        """测试注销 Provider"""
        manager = get_provider_manager()
        
        manager.register("temp", MockProvider, config={})
        assert "temp" in manager.list_providers()
        
        manager.unregister("temp")
        assert "temp" not in manager.list_providers()
    
    def test_reset(self):
        """测试重置"""
        manager = get_provider_manager()
        
        manager.register("r1", MockProvider, config={})
        manager.register("r2", MockProvider, config={})
        
        manager.reset()
        
        assert len(manager.list_providers()) == 0
        assert manager._current_provider is None


class TestOpenAIProvider:
    """OpenAI Provider 测试"""
    
    @pytest.mark.asyncio
    async def test_chat(self):
        """测试聊天"""
        with patch("infra.providers.openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_client.return_value = mock_instance
            
            # 模拟 API 响应
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = "Hello!"
            mock_instance.chat.completions.create = AsyncMock(return_value=mock_response)
            
            provider = OpenAIProvider(api_key="test-key")
            result = await provider.chat([{"role": "user", "content": "Hi"}])
            
            assert result == "Hello!"
            mock_instance.chat.completions.create.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_chat_stream(self):
        """测试流式聊天"""
        with patch("infra.providers.openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_client.return_value = mock_instance
            
            # 模拟流式响应
            async def mock_stream():
                for i in range(3):
                    chunk = MagicMock()
                    chunk.choices = [MagicMock()]
                    chunk.choices[0].delta.content = f"token{i}"
                    yield chunk
            
            mock_instance.chat.completions.create = AsyncMock(return_value=mock_stream())
            
            provider = OpenAIProvider(api_key="test-key")
            tokens = []
            async for delta in provider.chat_stream([{"role": "user", "content": "Hi"}]):
                tokens.append(delta.content)
            
            assert tokens == ["token0", "token1", "token2"]


class TestAnthropicProvider:
    """Anthropic Provider 测试"""
    
    @pytest.mark.asyncio
    async def test_convert_messages(self):
        """测试消息格式转换"""
        provider = AnthropicProvider(api_key="test-key")
        
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
        ]
        
        converted = provider._convert_messages(messages)
        
        # system 应该被过滤
        assert len(converted) == 1
        assert converted[0]["role"] == "user"
        assert converted[0]["content"] == "Hello"


class TestLocalProvider:
    """Local Provider (Ollama) 测试"""
    
    @pytest.mark.asyncio
    async def test_chat(self):
        """测试聊天"""
        with patch("infra.providers.local.httpx") as mock_httpx:
            mock_instance = MagicMock()
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_httpx.AsyncClient.return_value = mock_instance
            
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "message": {"content": "Ollama response"}
            }
            mock_instance.post = AsyncMock(return_value=mock_response)
            
            provider = LocalProvider(base_url="http://localhost:11434")
            result = await provider.chat([{"role": "user", "content": "Hi"}])
            
            assert result == "Ollama response"


class TestBaseProvider:
    """BaseProvider 测试"""
    
    @pytest.mark.asyncio
    async def test_chat_with_retry_success(self):
        """测试重试成功"""
        provider = MockProvider()
        
        result = await provider.chat_with_retry([{"role": "user", "content": "Hi"}])
        assert result == "mock response"
    
    @pytest.mark.asyncio
    async def test_chat_with_retry_failure(self):
        """测试重试失败"""
        class FailingProvider(MockProvider):
            call_count = 0
            
            async def chat(self, messages, **kwargs):
                FailingProvider.call_count += 1
                if FailingProvider.call_count < 3:
                    raise Exception("Temporary error")
                return "success after retry"
        
        provider = FailingProvider()
        
        with pytest.raises(RuntimeError, match="调用失败"):
            await provider.chat_with_retry([{"role": "user", "content": "Hi"}], max_retries=2)


class TestProviderIntegration:
    """Provider 集成测试"""
    
    def setup_method(self):
        reset_provider_manager()
    
    @pytest.mark.asyncio
    async def test_provider_manager_with_real_provider(self):
        """测试 ProviderManager 与 OpenAIProvider 配合"""
        with patch("infra.providers.openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_client.return_value = mock_instance
            
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = "Integration test"
            mock_instance.chat.completions.create = AsyncMock(return_value=mock_response)
            
            manager = get_provider_manager()
            manager.register(
                "openai",
                OpenAIProvider,
                config={"api_key": "test-key"},
                set_current=True,
            )
            
            provider = manager.get("openai")
            result = await provider.chat([{"role": "user", "content": "test"}])
            
            assert result == "Integration test"
    
    def test_singleton_pattern(self):
        """测试单例模式"""
        m1 = get_provider_manager()
        m2 = get_provider_manager()
        assert m1 is m2
