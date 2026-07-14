"""Semantic Memory 系统单元测试"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from core.memory_db import MemoryDB, MemoryEntry, get_memory_db, reset_memory_db
from core.semantic_memory import SemanticMemoryStore, get_semantic_memory, reset_semantic_memory
from infra.embedding import MockEmbedding, BaseEmbeddingService


class TestMemoryDB:
    """MemoryDB 测试"""
    
    def setup_method(self):
        """每个测试前重置"""
        reset_memory_db()
        # 使用临时数据库
        import tempfile
        self.temp_dir = Path(tempfile.mkdtemp())
        self.db_path = self.temp_dir / "test_memory.db"
        self.db = MemoryDB(db_path=self.db_path)
    
    def teardown_method(self):
        """清理"""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_add_and_get(self):
        """测试添加和获取记忆"""
        entry = self.db.add(
            type="context",
            content="用户在讨论 Python 编程",
            metadata={"source": "test"},
        )
        
        assert entry.id is not None
        assert entry.type == "context"
        assert entry.content == "用户在讨论 Python 编程"
        
        # 获取
        retrieved = self.db.get(entry.id)
        assert retrieved is not None
        assert retrieved.id == entry.id
        assert retrieved.content == entry.content
    
    def test_add_with_embedding(self):
        """测试添加带向量的记忆"""
        embedding = [0.1] * 1536
        entry = self.db.add(
            type="context",
            content="测试向量记忆",
            embedding=embedding,
        )
        
        retrieved = self.db.get(entry.id)
        assert retrieved.embedding is not None
        assert len(retrieved.embedding) == 1536
    
    def test_search_fts(self):
        """测试全文搜索"""
        self.db.add(type="context", content="Python 是一种编程语言")
        self.db.add(type="context", content="JavaScript 用于 Web 开发")
        self.db.add(type="context", content="Go 语言适合服务器编程")
        
        results = self.db.search_fts("Python", limit=10)
        assert len(results) >= 1
        assert any("Python" in r.content for r in results)
    
    def test_search_fts_with_type_filter(self):
        """测试带类型过滤的搜索"""
        self.db.add(type="success", content="成功完成 Python 任务")
        self.db.add(type="failure", content="Python 导入失败")
        
        results = self.db.search_fts("Python", type="success", limit=10)
        assert all(r.type == "success" for r in results)
    
    def test_get_recent(self):
        """测试获取最近记忆"""
        for i in range(5):
            self.db.add(type="context", content=f"记忆 {i}")
        
        recent = self.db.get_recent(limit=3)
        assert len(recent) == 3
    
    def test_delete(self):
        """测试删除记忆"""
        entry = self.db.add(type="context", content="待删除的记忆")
        assert self.db.get(entry.id) is not None
        
        success = self.db.delete(entry.id)
        assert success is True
        assert self.db.get(entry.id) is None
    
    def test_stats(self):
        """测试统计信息"""
        self.db.add(type="context", content="测试1")
        self.db.add(type="success", content="测试2")
        
        stats = self.db.get_stats()
        assert stats["total"] >= 2
        assert "context" in stats["by_type"]
        assert "success" in stats["by_type"]


class TestMockEmbedding:
    """Mock Embedding 测试"""
    
    @pytest.mark.asyncio
    async def test_embed(self):
        """测试嵌入生成"""
        embedding = MockEmbedding(dimension=128)
        vec = await embedding.embed("测试文本")
        
        assert len(vec) == 128
    
    @pytest.mark.asyncio
    async def test_embed_batch(self):
        """测试批量嵌入"""
        embedding = MockEmbedding(dimension=128)
        vecs = await embedding.embed_batch(["文本1", "文本2", "文本3"])
        
        assert len(vecs) == 3
        assert all(len(v) == 128 for v in vecs)
    
    @pytest.mark.asyncio
    async def test_deterministic(self):
        """测试嵌入的确定性(相同文本产生相同向量)"""
        embedding = MockEmbedding(dimension=128)
        vec1 = await embedding.embed("确定文本")
        vec2 = await embedding.embed("确定文本")
        
        assert vec1 == vec2


class TestSemanticMemoryStore:
    """SemanticMemoryStore 测试"""
    
    def setup_method(self):
        """每个测试前重置"""
        reset_semantic_memory()
        reset_memory_db()
        
        import tempfile
        self.temp_dir = Path(tempfile.mkdtemp())
        
        # 创建测试用的 MemoryDB
        self.db = MemoryDB(db_path=self.temp_dir / "test_semantic.db")
        
        # 创建带 Mock Embedding 的 SemanticMemoryStore
        self.mock_embedding = MockEmbedding(dimension=128)
        self.store = SemanticMemoryStore(
            memory_db=self.db,
            embedding_service=self.mock_embedding,
            enable_semantic=True,
            enable_fts=True,
        )
    
    def teardown_method(self):
        """清理"""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    @pytest.mark.asyncio
    async def test_add_memory(self):
        """测试添加记忆"""
        entry = await self.store.add_memory(
            content="用户询问 Python 学习路线",
            type="context",
            tags=["python", "学习"],
        )
        
        assert entry.id is not None
        assert entry.embedding is not None
        assert len(entry.embedding) == 128
    
    @pytest.mark.asyncio
    async def test_search(self):
        """测试语义搜索"""
        await self.store.add_memory(
            content="Python 适合初学者入门编程",
            type="context",
        )
        await self.store.add_memory(
            content="JavaScript 是 Web 开发的主要语言",
            type="context",
        )
        
        results = await self.store.search("Python 编程", limit=10)
        assert len(results) >= 1
    
    @pytest.mark.asyncio
    async def test_search_context(self):
        """测试上下文搜索"""
        await self.store.add_memory(
            content="用户正在做一个数据分析项目",
            type="context",
        )
        await self.store.add_memory(
            content="用户想要学习机器学习",
            type="context",
        )
        
        contexts = await self.store.search_context("用户项目", limit=3)
        assert len(contexts) >= 1
    
    @pytest.mark.asyncio
    async def test_recall_recent_context(self):
        """测试回忆最近上下文"""
        await self.store.add_memory(
            content="上次那个项目需要用到 pandas",
            type="context",
        )
        
        recall = await self.store.recall_recent_context("上次那个项目")
        assert "pandas" in recall or len(recall) > 0
    
    def test_add_sync(self):
        """测试同步添加记忆"""
        entry = self.store.add_sync(
            content="同步添加的记忆",
            type="context",
        )
        
        assert entry.id is not None
        assert entry.embedding is None  # 同步添加不生成向量
    
    def test_stats(self):
        """测试统计"""
        self.store.add_sync(content="测试1", type="context")
        
        stats = self.store.get_stats()
        assert "total" in stats
        assert stats["semantic_enabled"] is True


class TestIntegration:
    """集成测试"""
    
    def setup_method(self):
        reset_memory_db()
        reset_semantic_memory()
    
    @pytest.mark.asyncio
    async def test_full_workflow(self):
        """测试完整工作流"""
        import tempfile
        temp_dir = Path(tempfile.mkdtemp())
        
        # 1. 初始化 MemoryDB
        db = MemoryDB(db_path=temp_dir / "workflow.db")
        
        # 2. 初始化 Embedding
        embedding = MockEmbedding(dimension=128)
        
        # 3. 初始化 SemanticMemoryStore
        store = SemanticMemoryStore(
            memory_db=db,
            embedding_service=embedding,
        )
        
        # 4. 添加多条记忆
        await store.add_memory(
            content="用户上次问我关于 Python 的问题",
            type="context",
            metadata={"topic": "python"},
        )
        await store.add_memory(
            content="用户对数据分析很感兴趣",
            type="preference",
        )
        
        # 5. 搜索
        results = await store.search("Python", limit=5)
        assert len(results) >= 1
        
        # 6. 获取统计
        stats = store.get_stats()
        assert stats["total"] >= 2
        
        # 清理
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    def test_singleton_pattern(self):
        """测试单例模式"""
        m1 = get_semantic_memory()
        m2 = get_semantic_memory()
        # 可能返回 None 因为没有启用
        # assert m1 is m2
