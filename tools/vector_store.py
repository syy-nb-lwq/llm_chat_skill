"""向量存储 - 基于 ChromaDB"""
import os
import uuid
from pathlib import Path
from typing import Optional, List, Dict


class VectorStore:
    """向量存储"""
    
    def __init__(self, persist_directory: str = "vector_store"):
        self.persist_directory = Path(persist_directory)
        self.persist_directory.mkdir(exist_ok=True)
        
        import chromadb
        self.client = chromadb.PersistentClient(path=str(self.persist_directory))
        self.collection = self.client.get_or_create_collection(
            name="documents",
            metadata={"description": "文档向量存储"}
        )
        
        # 初始化 embedding 模型
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
    
    def add(self, text: str, metadata: dict = None, doc_id: str = None) -> str:
        """添加文档"""
        doc_id = doc_id or str(uuid.uuid4())
        embedding = self.model.encode(text).tolist()
        
        self.collection.add(
            ids=[doc_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[metadata or {}]
        )
        
        return doc_id
    
    def add_batch(self, texts: List[str], metadatas: List[dict] = None) -> List[str]:
        """批量添加"""
        doc_ids = [str(uuid.uuid4()) for _ in texts]
        embeddings = self.model.encode(texts).tolist()
        
        self.collection.add(
            ids=doc_ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas or [{}] * len(texts)
        )
        
        return doc_ids
    
    def search(self, query: str, top_k: int = 5, where: dict = None) -> List[Dict]:
        """向量检索"""
        query_embedding = self.model.encode(query).tolist()
        
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where
        )
        
        # 格式化结果
        output = []
        if results['ids'] and results['ids'][0]:
            for i, doc_id in enumerate(results['ids'][0]):
                output.append({
                    "id": doc_id,
                    "content": results['documents'][0][i],
                    "metadata": results['metadatas'][0][i] if results['metadatas'] else {},
                    "distance": results['distances'][0][i] if results['distances'] else 0
                })
        
        return output
    
    def delete(self, doc_id: str) -> bool:
        """删除文档"""
        try:
            self.collection.delete(ids=[doc_id])
            return True
        except:
            return False
    
    def get(self, doc_id: str) -> Optional[Dict]:
        """获取文档"""
        try:
            result = self.collection.get(ids=[doc_id])
            if result['ids']:
                return {
                    "id": result['ids'][0],
                    "content": result['documents'][0],
                    "metadata": result['metadatas'][0] if result['metadatas'] else {}
                }
        except:
            pass
        return None
    
    def list_all(self, limit: int = 100) -> List[Dict]:
        """列出所有文档"""
        result = self.collection.get(limit=limit)
        
        output = []
        if result['ids']:
            for i, doc_id in enumerate(result['ids']):
                output.append({
                    "id": doc_id,
                    "content": result['documents'][i][:200] + "..." if len(result['documents'][i]) > 200 else result['documents'][i],
                    "metadata": result['metadatas'][i] if result['metadatas'] else {}
                })
        
        return output
    
    def count(self) -> int:
        """文档数量"""
        return self.collection.count()
    
    def clear(self):
        """清空所有文档"""
        self.client.delete_collection("documents")
        self.collection = self.client.get_or_create_collection(
            name="documents",
            metadata={"description": "文档向量存储"}
        )


# 全局单例
_vector_store: Optional[VectorStore] = None


def get_vector_store() -> VectorStore:
    """获取向量存储实例"""
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStore()
    return _vector_store
