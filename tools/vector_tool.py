"""向量工具 - 供智能体调用"""
import json
from .vector_store import get_vector_store


def vector_tool(operation: str, text: str = None, query: str = None, 
                doc_id: str = None, top_k: int = 5, 
                metadata: dict = None) -> str:
    """
    向量存储工具
    
    参数:
    - operation: 操作类型 (add/search/delete/list/count/clear)
    - text: 要添加的文本
    - query: 检索查询
    - doc_id: 文档ID
    - top_k: 返回数量
    - metadata: 元数据
    
    返回:
    - 操作结果的字符串描述
    """
    store = get_vector_store()
    
    if operation == "add":
        if not text:
            return "请提供要添加的文本 (text 参数)"
        doc_id = store.add(text=text, metadata=metadata)
        return f"文档已添加，ID: {doc_id}"
    
    elif operation == "add_batch":
        if not text:
            return "请提供要添加的文本列表 (text 参数)"
        texts = text if isinstance(text, list) else [text]
        doc_ids = store.add_batch(texts=texts, metadatas=[metadata] if metadata else None)
        return f"已添加 {len(doc_ids)} 个文档，ID: {', '.join(doc_ids[:3])}..."
    
    elif operation == "search":
        if not query:
            return "请提供检索查询 (query 参数)"
        results = store.search(query=query, top_k=top_k)
        
        if not results:
            return "未找到相关结果"
        
        output = [f"找到 {len(results)} 条相关结果:\n"]
        for i, r in enumerate(results, 1):
            content = r['content'][:200] + "..." if len(r['content']) > 200 else r['content']
            distance = r.get('distance', 0)
            output.append(f"\n{i}. [ID: {r['id']}] (相似度: {1-distance:.2f})")
            output.append(f"   {content}")
        
        return "\n".join(output)
    
    elif operation == "delete":
        if not doc_id:
            return "请提供要删除的文档ID (doc_id 参数)"
        success = store.delete(doc_id)
        return "文档已删除" if success else "删除失败"
    
    elif operation == "get":
        if not doc_id:
            return "请提供文档ID (doc_id 参数)"
        doc = store.get(doc_id)
        if doc:
            return f"ID: {doc['id']}\n内容: {doc['content']}\n元数据: {doc['metadata']}"
        return "未找到文档"
    
    elif operation == "list":
        docs = store.list_all()
        if not docs:
            return "向量库为空"
        
        output = [f"向量库共有 {len(docs)} 条文档:\n"]
        for doc in docs[:10]:
            output.append(f"- [{doc['id'][:8]}...] {doc['content'][:50]}...")
        
        if len(docs) > 10:
            output.append(f"\n... 还有 {len(docs) - 10} 条")
        
        return "\n".join(output)
    
    elif operation == "count":
        count = store.count()
        return f"向量库共有 {count} 条文档"
    
    elif operation == "clear":
        store.clear()
        return "向量库已清空"
    
    else:
        return f"未知操作: {operation}。可用操作: add/search/delete/list/count/clear"
