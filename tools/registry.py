"""工具注册机制"""
import json
import os
import hashlib
from typing import Callable, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict
    func: Callable = field(repr=False)


class ToolRegistry:
    _instance = None
    _tools = {}
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tools = {}
            cls._instance._cache = {}
            cls._instance._cache_ttl = timedelta(minutes=30)
        return cls._instance
    
    def register(self, name: str, description: str, parameters: dict) -> Callable:
        def decorator(func: Callable) -> Callable:
            self._tools[name] = ToolDefinition(name=name, description=description, parameters=parameters, func=func)
            return func
        return decorator
    
    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)
    
    def list_all(self):
        return list(self._tools.values())
    
    def get_openai_format(self) -> list:
        return [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in self._tools.values()
        ]
    
    def execute(self, name: str, arguments: dict) -> Any:
        tool = self.get(name)
        if not tool:
            raise ValueError(f"工具不存在: {name}")
        return tool.func(**arguments)


registry = ToolRegistry()


# ============ 工具定义 ============

@registry.register(
    name="fetch_webpage",
    description="获取网页内容。根据URL读取标题和正文。",
    parameters={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "网页URL"}},
        "required": ["url"]
    }
)
def fetch_webpage(url: str) -> str:
    from tools.fetch import fetch_webpage as _fetch
    result = _fetch(url)
    if result["success"]:
        return f"标题: {result['title']}\n\n内容:\n{result['text'][:20000]}"
    return f"获取失败: {result.get('error', '未知错误')}"


@registry.register(
    name="read_file",
    description="读取本地文本文件。",
    parameters={
        "type": "object",
        "properties": {"file_path": {"type": "string", "description": "文件路径"}},
        "required": ["file_path"]
    }
)
def read_file(file_path: str) -> str:
    if not os.path.exists(file_path):
        return f"文件不存在: {file_path}"
    ext = os.path.splitext(file_path)[1].lower()
    supported = ['.txt', '.md', '.json', '.csv', '.py', '.js', '.html', '.xml', '.yaml', '.yml', '.sql', '.log']
    if ext not in supported:
        return f"不支持的文件类型: {ext}"
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        if len(content) > 50000:
            content = content[:50000] + "\n\n[已截断]"
        return f"文件: {os.path.basename(file_path)}\n\n{content}"
    except Exception as e:
        return f"读取失败: {str(e)}"


@registry.register(
    name="read_pdf",
    description="读取PDF文档。",
    parameters={
        "type": "object",
        "properties": {"file_path": {"type": "string", "description": "PDF路径"}},
        "required": ["file_path"]
    }
)
def read_pdf(file_path: str) -> str:
    if not os.path.exists(file_path):
        return f"文件不存在: {file_path}"
    if not file_path.lower().endswith('.pdf'):
        return f"不是PDF文件"
    try:
        from PyPDF2 import PdfReader
        with open(file_path, 'rb') as f:
            reader = PdfReader(f)
            parts = []
            for i, page in enumerate(reader.pages[:20]):
                text = page.extract_text()
                if text and text.strip():
                    parts.append(f"--- 第{i+1}/{len(reader.pages)}页 ---\n{text}")
            content = "\n\n".join(parts)
        if not content.strip():
            return f"PDF共{len(reader.pages)}页，无法提取文字"
        if len(content) > 50000:
            content = content[:50000] + "\n\n[已截断]"
        return f"PDF: {os.path.basename(file_path)} (共{len(reader.pages)}页)\n\n{content}"
    except ImportError:
        return "缺少PyPDF2"
    except Exception as e:
        return f"PDF读取失败: {str(e)}"


@registry.register(
    name="read_image",
    description="识别图片中的文字。",
    parameters={
        "type": "object",
        "properties": {"file_path": {"type": "string", "description": "图片路径"}},
        "required": ["file_path"]
    }
)
def read_image(file_path: str) -> str:
    if not os.path.exists(file_path):
        return f"文件不存在: {file_path}"
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']:
        return f"不支持的图片类型: {ext}"
    try:
        import base64
        from config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
        from openai import OpenAI
        with open(file_path, 'rb') as f:
            image_data = f.read()
        base64_image = base64.b64encode(image_data).decode('utf-8')
        client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/{ext[1:]};base64,{base64_image}"}},
                    {"type": "text", "text": "请提取这张图片中的所有文字内容。"}
                ]
            }],
            max_tokens=4000
        )
        return f"图片: {os.path.basename(file_path)}\n\n识别内容:\n{response.choices[0].message.content}"
    except Exception as e:
        if "unknown variant" in str(e):
            return "当前模型不支持图片识别"
        return f"OCR失败: {str(e)}"


@registry.register(
    name="answer_question",
    description="根据内容回答问题。",
    parameters={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要分析的内容"},
            "question": {"type": "string", "description": "要回答的问题"}
        },
        "required": ["content", "question"]
    }
)
def answer_question(content: str, question: str) -> str:
    from config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
    from openai import OpenAI
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": "你是助手，根据内容回答问题。"},
            {"role": "user", "content": f"内容:\n{content}\n\n问题: {question}"}
        ],
        max_tokens=1000
    )
    return response.choices[0].message.content


@registry.register(
    name="memory",
    description="用户记忆系统。管理用户偏好和学习记录。",
    parameters={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string", 
                "description": "操作类型: recall(检索记忆)/save(保存记忆)/profile(查看画像)/clear(清除记忆)/learn(学习新信息)"
            },
            "key": {"type": "string", "description": "记忆键"},
            "value": {"type": "string", "description": "记忆值"},
            "query": {"type": "string", "description": "检索关键词"},
            "event_type": {"type": "string", "description": "学习事件类型: tool_use/file_type/topic/context"},
            "event_data": {"type": "object", "description": "事件数据"}
        }
    }
)
def memory(operation: str, key: str = None, value: str = None, query: str = None,
           event_type: str = None, event_data: dict = None) -> str:
    from tools.memory_tool import memory_tool
    return memory_tool(
        operation=operation, key=key, value=value, query=query,
        event_type=event_type, event_data=event_data
    )


@registry.register(
    name="vector_add",
    description="添加文本到向量库。用于持久化存储重要信息。",
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要存储的文本内容"},
            "metadata": {"type": "object", "description": "关联的元数据，如来源、时间等"}
        },
        "required": ["text"]
    }
)
def vector_add(text: str, metadata: dict = None) -> str:
    from tools.vector_tool import vector_tool
    return vector_tool(operation="add", text=text, metadata=metadata)


@registry.register(
    name="vector_search",
    description="在向量库中检索相关内容。基于语义相似度搜索。",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索查询"},
            "top_k": {"type": "integer", "description": "返回数量，默认5"}
        },
        "required": ["query"]
    }
)
def vector_search(query: str, top_k: int = 5) -> str:
    from tools.vector_tool import vector_tool
    return vector_tool(operation="search", query=query, top_k=top_k)


@registry.register(
    name="vector_list",
    description="列出向量库中的所有文档。",
    parameters={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "返回数量，默认100"}
        }
    }
)
def vector_list(limit: int = 100) -> str:
    from tools.vector_tool import vector_tool
    return vector_tool(operation="list")


@registry.register(
    name="vector_delete",
    description="从向量库删除文档。",
    parameters={
        "type": "object",
        "properties": {
            "doc_id": {"type": "string", "description": "文档ID"}
        },
        "required": ["doc_id"]
    }
)
def vector_delete(doc_id: str) -> str:
    from tools.vector_tool import vector_tool
    return vector_tool(operation="delete", doc_id=doc_id)


@registry.register(
    name="vector_count",
    description="获取向量库中的文档数量。",
    parameters={
        "type": "object",
        "properties": {}
    }
)
def vector_count() -> str:
    from tools.vector_tool import vector_tool
    return vector_tool(operation="count")


# ============ 代码执行和技能学习工具 ============

@registry.register(
    name="run_code",
    description="执行 Python 代码。用于运行 Agent 编写的代码。",
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "要执行的 Python 代码"}
        },
        "required": ["code"]
    }
)
def run_code_tool(code: str) -> str:
    from tools.skill_tools import execute_code
    return execute_code(code)


@registry.register(
    name="learn_skill",
    description="从需求描述学习并创建新技能。",
    parameters={
        "type": "object",
        "properties": {
            "requirement": {"type": "string", "description": "技能需求描述"}
        },
        "required": ["requirement"]
    }
)
def learn_skill_tool(requirement: str) -> str:
    from tools.skill_tools import learn_skill
    return learn_skill(requirement)


@registry.register(
    name="create_skill",
    description="创建新技能。将代码保存为可复用的技能。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名称"},
            "description": {"type": "string", "description": "技能描述"},
            "code": {"type": "string", "description": "Python 函数代码"},
            "parameters": {"type": "object", "description": "参数定义"}
        },
        "required": ["name", "description", "code"]
    }
)
def create_skill_tool(name: str, description: str, code: str, parameters: dict = None) -> str:
    from tools.skill_tools import create_skill
    return create_skill(name, description, code, parameters or {})


@registry.register(
    name="list_skills",
    description="列出所有已加载的技能。",
    parameters={
        "type": "object",
        "properties": {}
    }
)
def list_skills_tool() -> str:
    from tools.skill_tools import list_skills
    return list_skills()


@registry.register(
    name="load_skill",
    description="加载指定技能，获取其代码。",
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "技能ID"}
        },
        "required": ["skill_id"]
    }
)
def load_skill_tool(skill_id: str) -> str:
    from tools.skill_tools import load_skill
    return load_skill(skill_id)


@registry.register(
    name="delete_skill",
    description="删除指定技能。",
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "技能ID"}
        },
        "required": ["skill_id"]
    }
)
def delete_skill_tool(skill_id: str) -> str:
    from tools.skill_tools import delete_skill
    return delete_skill(skill_id)


@registry.register(
    name="execute_skill",
    description="执行指定技能中的函数。",
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "技能ID"},
            "func_name": {"type": "string", "description": "函数名称"},
            "params": {"type": "object", "description": "函数参数"}
        },
        "required": ["skill_id", "func_name"]
    }
)
def execute_skill_tool(skill_id: str, func_name: str, params: dict = None) -> str:
    from tools.skill_tools import execute_skill_function
    return execute_skill_function(skill_id, func_name, params)


# ============ 文件操作工具 ============

@registry.register(
    name="read_file",
    description="读取本地文件内容。",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件路径"},
            "max_chars": {"type": "integer", "description": "最大读取字符数，默认5000"}
        },
        "required": ["file_path"]
    }
)
def read_file_tool(file_path: str, max_chars: int = 5000) -> str:
    """读取文件内容"""
    try:
        if not os.path.exists(file_path):
            return f"❌ 文件不存在: {file_path}"
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n[文件过长，已截断到前{max_chars}字符]"
        
        return f"✅ 文件读取成功\n\n文件: {os.path.basename(file_path)}\n大小: {len(content)} 字符\n\n内容:\n{content}"
    except Exception as e:
        return f"❌ 读取失败: {str(e)}"


@registry.register(
    name="save_file",
    description="保存内容到文件，并返回文件路径。",
    parameters={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要保存的内容"},
            "file_name": {"type": "string", "description": "文件名，如 output.xlsx, result.json"},
            "directory": {"type": "string", "description": "保存目录，默认 outputs"}
        },
        "required": ["content", "file_name"]
    }
)
def save_file_tool(content: str, file_name: str, directory: str = "outputs") -> str:
    """保存文件"""
    try:
        # 创建目录
        os.makedirs(directory, exist_ok=True)
        
        # 处理内容
        file_path = os.path.join(directory, file_name)
        
        # 根据文件类型处理
        if file_name.endswith('.xlsx') or file_name.endswith('.xls'):
            # Excel 文件 - content 应该是文件路径
            if os.path.exists(content):
                import shutil
                shutil.copy(content, file_path)
                return f"✅ Excel 文件已保存\n\n📁 路径: {os.path.abspath(file_path)}"
        
        elif file_name.endswith('.json'):
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
        
        elif file_name.endswith('.txt') or file_name.endswith('.md'):
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
        
        else:
            # 默认作为文本保存
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
        
        return f"✅ 文件已保存\n\n📁 路径: {os.path.abspath(file_path)}\n📝 提示: 用户可以在此路径找到生成的文件"
    except Exception as e:
        return f"❌ 保存失败: {str(e)}"


@registry.register(
    name="list_files",
    description="列出目录下的文件。",
    parameters={
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "目录路径，默认 outputs"},
            "pattern": {"type": "string", "description": "文件匹配模式，如 *.xlsx"}
        }
    }
)
def list_files_tool(directory: str = "outputs", pattern: str = "*") -> str:
    """列出文件"""
    try:
        if not os.path.exists(directory):
            return f"📂 目录不存在: {directory}"
        
        import glob
        files = glob.glob(os.path.join(directory, pattern))
        
        if not files:
            return f"📂 目录 {directory} 中没有匹配的文件"
        
        output = [f"📂 目录 {directory} 中的文件:\n"]
        for f in files:
            size = os.path.getsize(f)
            mtime = datetime.fromtimestamp(os.path.getmtime(f)).strftime('%Y-%m-%d %H:%M')
            output.append(f"  📄 {os.path.basename(f)} ({size} bytes, {mtime})")
        
        return "\n".join(output)
    except Exception as e:
        return f"❌ 列出文件失败: {str(e)}"


@registry.register(
    name="get_download_path",
    description="获取文件的下载路径，用于返回给用户。",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件路径"}
        },
        "required": ["file_path"]
    }
)
def get_download_path_tool(file_path: str) -> str:
    """获取下载路径"""
    try:
        if not os.path.exists(file_path):
            return f"❌ 文件不存在: {file_path}"
        
        abs_path = os.path.abspath(file_path)
        file_name = os.path.basename(file_path)
        size = os.path.getsize(file_path)
        
        return f"""📥 文件下载信息

文件名: {file_name}
路径: {abs_path}
大小: {size} bytes

请告诉用户可以在此路径找到文件。"""
    except Exception as e:
        return f"❌ 获取路径失败: {str(e)}"


__all__ = ['registry', 'ToolRegistry', 'ToolDefinition']
