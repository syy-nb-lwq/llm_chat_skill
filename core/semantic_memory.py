"""SemanticMemoryStore - 语义记忆存储,支持语义检索

分层(docs/13 §2.1):
- 短期记忆 = 会话内容(`core/session_memory.py`),不进这里
- 长期记忆 = 用户画像(`preference/habit/method/constraint/fact`),进这里
"""
from typing import Any, Dict, List, Optional, Tuple

from core.memory_db import (
    PROFILE_MEMORY_TYPES,
    MemoryDB,
    MemoryEntry,
    get_memory_db,
)
from infra.embedding import BaseEmbeddingService
from infra.logger import get_logger


#: RRF 常数(经验值 60,见 Cormack et al. 2009)
RRF_K = 60

#: 画像常驻注入的字符预算(粗略 ≈ token × 1.5)
PROFILE_BLOCK_MAX_CHARS = 400

#: 向量通道的默认最小余弦相似度。
#:
#: 不能是 0 —— 那样任何带向量的记忆都会进融合,随机分数会盖掉全文检索的
#: 真实命中(实测:mock 向量下查"北京天气"返回"周报要有结论")。
DEFAULT_MIN_SIMILARITY = 0.35


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
        min_similarity: float = DEFAULT_MIN_SIMILARITY,
    ):
        self.memory_db = memory_db or get_memory_db()
        self.embedding_service = None
        self.enable_semantic = False
        self.enable_fts = enable_fts
        self.min_similarity = min_similarity
        self.logger = get_logger()
        if embedding_service is not None:
            self.set_embedding_service(embedding_service, force=enable_semantic)

    def set_embedding_service(
        self, service: BaseEmbeddingService, *, force: bool = False
    ) -> bool:
        """绑定嵌入服务。

        **合成向量(如 MockEmbedding)默认被拒绝** —— 它的余弦相似度与语义无关
        (实测任意两句 ≈ 0.81),用来做语义检索会把全文检索的真实命中挤掉。

        Args:
            service: 嵌入服务
            force: True 时无视合成标记(仅供测试构造检索路径)

        Returns:
            是否真的启用了语义通道
        """
        self.embedding_service = service
        if getattr(service, "is_synthetic", False) and not force:
            self.enable_semantic = False
            self.logger.warning(
                "SemanticMemoryStore",
                f"嵌入服务 {type(service).__name__} 是合成向量,"
                "已禁用语义通道(只用 FTS)。要真语义检索请配 EMBEDDING_PROVIDER=openai/local",
            )
            return False
        self.enable_semantic = True
        return True

    async def add_memory(
        self,
        content: str,
        type: str = "context",
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        generate_embedding: bool = True,
        scope: str = "user",
    ) -> MemoryEntry:
        """添加记忆
        
        Args:
            content: 记忆内容
            type: 记忆类型 (failure/success/preference/context)
            metadata: 额外元数据
            tags: 标签
            generate_embedding: 是否生成向量(需要 embedding_service)
            scope: 作用域（session/user/project/global/agent）。
                **之前写路径恒为 user**，检索也一律不过滤 —— scope 是死字段
                (docs/18 F0-4)。现在写进来的就是真作用域。
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
            scope=scope,
        )

    async def search(
        self,
        query: str,
        *,
        types: Optional[List[str]] = None,
        type: Optional[str] = None,
        limit: int = 5,
        scopes: Optional[List[str]] = None,
    ) -> List[Tuple[MemoryEntry, float]]:
        """混合检索:向量通道 + 全文通道,**RRF 融合**。

        为什么用 RRF 而不是加权求和(docs/13 §2.5 ④):
        向量分是 0~1 的余弦相似度,FTS 的 BM25 是任意量纲的负值 ——
        两者相加必须先归一化,而归一化本身没有可靠依据。
        RRF 只看**名次**,天然免疫量纲问题,且无参数敏感。

        Args:
            query: 查询串
            types: 记忆类型过滤(多个)
            type: 记忆类型过滤(单个,兼容旧调用)
            limit: 返回条数
            scopes: 作用域过滤（session/user/project/global/agent）。
                不传 = 不按作用域过滤（保持旧行为）。

        Returns:
            [(MemoryEntry, rrf_score)],按分数降序
        """
        if not query or not query.strip():
            return []

        type_filter = [type] if type else (types or None)

        entries: Dict[str, MemoryEntry] = {}
        ranks: Dict[str, List[int]] = {}

        def _record(rank: int, entry: MemoryEntry) -> None:
            entries[entry.id] = entry
            ranks.setdefault(entry.id, []).append(rank)

        # ---- 通道 1:向量 ----
        if self.enable_semantic and self.embedding_service:
            try:
                query_embedding = await self.embedding_service.embed(query)
                vector_hits = self.memory_db.search_vector(
                    query_embedding=query_embedding,
                    types=type_filter,
                    scopes=scopes,
                    limit=limit * 4,
                    threshold=self.min_similarity,
                )
                for rank, (entry, _score) in enumerate(vector_hits):
                    _record(rank, entry)
            except Exception as e:
                self.logger.warning("SemanticMemoryStore", f"语义检索失败(降级 FTS): {e}")

        # ---- 通道 2:全文(FTS5,擅长英文/数字词) ----
        if self.enable_fts:
            try:
                fts_hits = self.memory_db.search_fts(
                    query=query,
                    types=type_filter,
                    scopes=scopes,
                    limit=limit * 4,
                )
                for rank, (entry, _score) in enumerate(fts_hits):
                    _record(rank, entry)
            except Exception as e:
                self.logger.warning("SemanticMemoryStore", f"全文检索失败: {e}")

        # ---- 通道 3:CJK 二元组(Python 侧) ----
        # FTS5 的 unicode61 切不开中文,这一条通道才是中文场景的主力。
        # 不能省 —— 少了它,中文查询在 FTS-only 模式下**一条都搜不到**。
        try:
            cjk_hits = self.memory_db.search_cjk(
                query=query,
                types=type_filter,
                scopes=scopes,
                limit=limit * 4,
            )
            for rank, (entry, _score) in enumerate(cjk_hits):
                _record(rank, entry)
        except Exception as e:
            self.logger.warning("SemanticMemoryStore", f"CJK 检索失败: {e}")

        # ---- RRF 融合:每命中一个通道第 r 名,加 1/(k+r) ----
        fused: List[Tuple[MemoryEntry, float]] = []
        for entry_id, rank_list in ranks.items():
            score = sum(1.0 / (RRF_K + rank + 1) for rank in rank_list)
            fused.append((entries[entry_id], score))

        fused.sort(key=lambda x: -x[1])
        return fused[:limit]

    async def search_context(
        self, query: str, *, limit: int = 3,
        scopes: Optional[List[str]] = None,
    ) -> List[str]:
        """检索与本轮相关的「执行经验」类记忆,返回可注入的文本片段。

        只取 episode / failure / context —— 用户画像是**常驻注入**的,
        走 `build_profile_block()`,不在这里按需召回(docs/13 §2.8)。
        """
        results = await self.search(
            query=query,
            types=["episode", "failure", "context"],
            limit=limit,
            scopes=scopes,
        )

        contexts = []
        for entry, _score in results:
            contexts.append(f"[相关记忆 {entry.type}] {entry.content}")

        return contexts

    async def search_preferences(self, limit: int = 5) -> List[str]:
        """获取用户偏好(画像类)"""
        recent = self.memory_db.get_recent(
            types=list(PROFILE_MEMORY_TYPES),
            limit=limit,
        )
        return [f"[{entry.type}] {entry.content}" for entry in recent]

    async def recall_recent_context(
        self, query: str, *, limit: int = 5
    ) -> str:
        """回忆最近的上下文记忆

        用于处理"上次那个项目"等指代
        """
        results = await self.search(
            query=query,
            types=["episode", "context"],
            limit=limit,
        )

        if not results:
            return ""

        context_lines = ["=== 最近的上下文 ==="]
        for entry, _score in results:
            context_lines.append(f"- {entry.content}")
            if entry.metadata:
                context_lines.append(f"  (元数据: {entry.metadata})")

        return "\n".join(context_lines)

    # ---- 用户画像(长期记忆 L2) ----

    def get_profile(
        self, limit: int = 20
    ) -> List[MemoryEntry]:
        """取某用户的画像条目(按 置信度 × 重要度 排序)

        这是长期记忆的**常驻注入**来源:每轮都在场,不依赖 query 命中。
        """
        entries = self.memory_db.get_recent(
            types=list(PROFILE_MEMORY_TYPES),
            limit=max(limit * 2, 20),
        )
        entries.sort(
            key=lambda e: (e.confidence * 0.6 + e.importance * 0.4, e.created_at),
            reverse=True,
        )
        return entries[:limit]

    def build_profile_block(
        self, limit: int = 20, max_chars: int = PROFILE_BLOCK_MAX_CHARS
    ) -> str:
        """把用户画像拼成可注入提示词的文本块

        预算上限按字符数卡,超了就截断 —— 画像最多 ~400 字符,
        不跟会话内容抢 token(docs/13 §2.8 召回策略)。
        """
        entries = self.get_profile(limit=limit)
        if not entries:
            return ""

        label = {
            "preference": "偏好",
            "habit": "习惯",
            "method": "方法",
            "constraint": "雷区",
            "fact": "事实",
        }
        lines: List[str] = []
        used = 0
        for entry in entries:
            line = f"- [{label.get(entry.type, entry.type)}] {entry.content}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line) + 1
        if not lines:
            return ""

        return "[用户画像](长期记忆,请始终遵守)\n" + "\n".join(lines)

    def add_sync(
        self,
        content: str,
        type: str = "context",
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> MemoryEntry:
        """同步添加记忆(不生成向量)"""
        return self.memory_db.add(
            type=type,
            content=content,
            metadata=metadata,
            tags=tags,
            embedding=None,
            **kwargs,
        )

    def get_recent(
        self,
        type: Optional[str] = None,
        limit: int = 20,
        types: Optional[List[str]] = None,
    ) -> List[MemoryEntry]:
        """获取最近的记忆"""
        return self.memory_db.get_recent(
            type=type, limit=limit, types=types
        )

    def delete(self, id: str) -> bool:
        """硬删除记忆(连带清索引)"""
        return self.memory_db.delete(id)

    def forget(self, id: str) -> bool:
        """「忘掉这件事」:软删除 + 同步清索引(删除后不再被召回)"""
        return self.memory_db.soft_delete(id)

    def cleanup(self, days: int = 90) -> int:
        """清理过期记忆(画像类不参与清理)"""
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
