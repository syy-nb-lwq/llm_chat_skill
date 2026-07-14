"""SemanticMemoryStore - 语义记忆存储,支持语义检索"""
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import asdict

from core.memory_db import MemoryDB, MemoryEntry, get_memory_db
from infra.embedding import BaseEmbeddingService, get_embedding_service
from infra.logger import get_logger


class SemanticMemoryStore:
    """语义记忆存储
    
    结合 MemoryDB 和 Embedding 服务,提供:
    - 语义检索: 根据用户输入的语义查找相关记忆
    - 全文搜索: FTS5 关键词匹配
    - 向量相似度: Embedding 余弦相似度
    - 混合搜索: 结合关键词和语义
    """

    def __init__(
        self,
        memory_db: Optional[MemoryDB] = None,
        embedding_service: Optional[BaseEmbeddingService] = None,
        enable_semantic: bool = True,
        enable_fts: bool = True,
    ):
        self.memory_db = memory_db or get_memory_db()
        self.embedding_service = embedding_service
        self.enable_semantic = enable_semantic
        self.enable_fts = enable_fts
        self.logger = get_logger()

    def set_embedding_service(self, service: BaseEmbeddingService) -> None:
        """设置嵌入服务"""
        self.embedding_service = service
        self.enable_semantic = True

    async def add_memory(
        self,
        content: str,
        type: str = "context",
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        user_id: Optional[str] = None,
        generate_embedding: bool = True,
    ) -> MemoryEntry:
        """添加记忆
        
        Args:
            content: 记忆内容
            type: 记忆类型 (failure/success/preference/context)
            metadata: 额外元数据
            tags: 标签
            user_id: 用户 ID
            generate_embedding: 是否生成向量(需要 embedding_service)
        """
        embedding = None
        
        if generate_embedding and self.embedding_service:
            try:
                embedding = await self.embedding_service.embed(content)
            except Exception as e:
                self.logger.warning("SemanticMemoryStore", f"生成嵌入向量失败: {e}")
        
        return self.memory_db.add(
            type=type,
            content=content,
            metadata=metadata,
            embedding=embedding,
            tags=tags,
            user_id=user_id,
        )

    async def search(
        self,
        query: str,
        type: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 5,
        semantic_weight: float = 0.7,
        fts_weight: float = 0.3,
    ) -> List[Tuple[MemoryEntry, float]]:
        """混合搜索:结合语义和全文搜索
        
        Args:
            query: 搜索查询
            type: 过滤记忆类型
            user_id: 过滤用户
            limit: 返回数量
            semantic_weight: 语义搜索权重
            fts_weight: 全文搜索权重
            
        Returns:
            List of (MemoryEntry, combined_score)
        """
        results: Dict[str, Tuple[MemoryEntry, float]] = {}
        
        # 语义搜索
        if self.enable_semantic and self.embedding_service:
            try:
                query_embedding = await self.embedding_service.embed(query)
                semantic_results = self.memory_db.search_vector(
                    query_embedding=query_embedding,
                    type=type,
                    limit=limit * 2,
                )
                for entry, score in semantic_results:
                    results[entry.id] = (entry, score * semantic_weight)
            except Exception as e:
                self.logger.warning("SemanticMemoryStore", f"语义搜索失败: {e}")
        
        # 全文搜索
        if self.enable_fts:
            try:
                fts_results = self.memory_db.search_fts(
                    query=query,
                    type=type,
                    limit=limit * 2,
                )
                max_fts_score = 1.0
                for entry in fts_results:
                    if entry.id in results:
                        # 合并分数
                        current = results[entry.id]
                        results[entry.id] = (current[0], current[1] + fts_weight * max_fts_score)
                    else:
                        results[entry.id] = (entry, fts_weight * max_fts_score)
            except Exception as e:
                self.logger.warning("SemanticMemoryStore", f"全文搜索失败: {e}")
        
        # 按分数排序
        sorted_results = sorted(results.values(), key=lambda x: -x[1])
        return sorted_results[:limit]

    async def search_context(self, query: str, user_id: Optional[str] = None, limit: int = 3) -> List[str]:
        """搜索上下文相关记忆,返回格式化字符串
        
        用于将记忆注入到 LLM prompt 中
        """
        results = await self.search(
            query=query,
            type="context",
            user_id=user_id,
            limit=limit,
        )
        
        contexts = []
        for entry, score in results:
            contexts.append(f"[相关记忆 {entry.type}] {entry.content}")
        
        return contexts

    async def search_preferences(self, user_id: str, limit: int = 5) -> List[str]:
        """获取用户偏好"""
        recent = self.memory_db.get_recent(
            type="preference",
            user_id=user_id,
            limit=limit,
        )
        return [f"[偏好] {entry.content}" for entry in recent]

    async def recall_recent_context(self, query: str, limit: int = 5) -> str:
        """回忆最近的上下文记忆
        
        用于处理"上次那个项目"等指代
        """
        results = await self.search(
            query=query,
            type="context",
            limit=limit,
        )
        
        if not results:
            return ""
        
        context_lines = ["=== 最近的上下文 ==="]
        for entry, score in results:
            context_lines.append(f"- {entry.content}")
            if entry.metadata:
                context_lines.append(f"  (元数据: {entry.metadata})")
        
        return "\n".join(context_lines)

    def add_sync(
        self,
        content: str,
        type: str = "context",
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        user_id: Optional[str] = None,
    ) -> MemoryEntry:
        """同步添加记忆(不生成向量)"""
        return self.memory_db.add(
            type=type,
            content=content,
            metadata=metadata,
            tags=tags,
            user_id=user_id,
            embedding=None,
        )

    def get_recent(self, type: Optional[str] = None, user_id: Optional[str] = None, limit: int = 20) -> List[MemoryEntry]:
        """获取最近的记忆"""
        return self.memory_db.get_recent(type=type, user_id=user_id, limit=limit)

    def delete(self, id: str) -> bool:
        """删除记忆"""
        return self.memory_db.delete(id)

    def cleanup(self, days: int = 90) -> int:
        """清理过期记忆"""
        return self.memory_db.cleanup_old(days=days)

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        db_stats = self.memory_db.get_stats()
        return {
            **db_stats,
            "semantic_enabled": self.enable_semantic and self.embedding_service is not None,
            "fts_enabled": self.enable_fts,
        }


# ---- 全局单例 ----
_semantic_memory: Optional[SemanticMemoryStore] = None


def get_semantic_memory() -> SemanticMemoryStore:
    """获取 SemanticMemoryStore 全局实例"""
    global _semantic_memory
    if _semantic_memory is None:
        _semantic_memory = SemanticMemoryStore()
    return _semantic_memory


def reset_semantic_memory() -> None:
    """重置全局实例"""
    global _semantic_memory
    _semantic_memory = None
