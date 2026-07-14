"""Embedding 服务封装 - 支持多种嵌入模型"""
from abc import ABC, abstractmethod
from typing import List, Optional
import httpx


class BaseEmbeddingService(ABC):
    """嵌入服务抽象基类"""
    
    @property
    @abstractmethod
    def dimension(self) -> int:
        """向量维度"""
        ...
    
    @abstractmethod
    async def embed(self, text: str) -> List[float]:
        """将单条文本转为向量"""
        ...
    
    @abstractmethod
    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量将文本转为向量"""
        ...


class OpenAIEmbedding(BaseEmbeddingService):
    """OpenAI Embedding 服务"""
    
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "text-embedding-3-small",
        dimension: int = 1536,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._dimension = dimension
    
    @property
    def dimension(self) -> int:
        return self._dimension
    
    async def embed(self, text: str) -> List[float]:
        """获取单条文本的嵌入向量"""
        results = await self.embed_batch([text])
        return results[0]
    
    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量获取嵌入向量"""
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "input": texts,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            
            # 按输入顺序返回
            embeddings = [item["embedding"] for item in data["data"]]
            
            # 如果指定了维度,进行截断
            if self._dimension and self._dimension < len(embeddings[0]):
                embeddings = [e[:self._dimension] for e in embeddings]
            
            return embeddings


class LocalEmbedding(BaseEmbeddingService):
    """本地嵌入服务 (如 Ollama with nomic-embed-text)"""
    
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
        dimension: int = 768,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._dimension = dimension
    
    @property
    def dimension(self) -> int:
        return self._dimension
    
    async def embed(self, text: str) -> List[float]:
        results = await self.embed_batch([text])
        return results[0]
    
    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Ollama 嵌入 API"""
        async with httpx.AsyncClient(timeout=120) as client:
            embeddings = []
            for text in texts:
                resp = await client.post(
                    f"{self.base_url}/api/embeddings",
                    json={
                        "model": self.model,
                        "prompt": text,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                embeddings.append(data["embedding"])
            return embeddings


class MockEmbedding(BaseEmbeddingService):
    """Mock 嵌入服务(用于测试)"""
    
    def __init__(self, dimension: int = 1536):
        self._dimension = dimension
    
    @property
    def dimension(self) -> int:
        return self._dimension
    
    async def embed(self, text: str) -> List[float]:
        # 返回假的向量(全 0.1)
        import hashlib
        # 基于文本内容生成确定性向量
        hash_val = int(hashlib.md5(text.encode()).hexdigest(), 16)
        import random
        random.seed(hash_val)
        return [random.random() for _ in range(self._dimension)]
    
    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [await self.embed(text) for text in texts]


# ---- 全局单例管理 ----
_embedding_service: Optional[BaseEmbeddingService] = None


def get_embedding_service() -> Optional[BaseEmbeddingService]:
    """获取嵌入服务全局实例"""
    global _embedding_service
    return _embedding_service


def init_embedding_service(
    provider: str = "openai",
    api_key: str = "",
    base_url: str = "",
    model: str = "",
    dimension: int = 1536,
) -> BaseEmbeddingService:
    """初始化嵌入服务
    
    Args:
        provider: "openai" | "local" | "mock"
        api_key: API Key (OpenAI)
        base_url: Base URL
        model: 模型名称
        dimension: 向量维度
    """
    global _embedding_service
    
    if provider == "openai":
        _embedding_service = OpenAIEmbedding(
            api_key=api_key,
            base_url=base_url or "https://api.openai.com/v1",
            model=model or "text-embedding-3-small",
            dimension=dimension,
        )
    elif provider == "local":
        _embedding_service = LocalEmbedding(
            base_url=base_url or "http://localhost:11434",
            model=model or "nomic-embed-text",
            dimension=dimension,
        )
    elif provider == "mock":
        _embedding_service = MockEmbedding(dimension=dimension)
    else:
        raise ValueError(f"Unknown embedding provider: {provider}")
    
    return _embedding_service


def reset_embedding_service() -> None:
    """重置嵌入服务"""
    global _embedding_service
    _embedding_service = None
