"""文件插件"""
import os
from pathlib import Path

from core.plugin import BasePlugin, ToolSchema, ToolResult


class FilePlugin(BasePlugin):
    """文件处理插件"""
    
    @property
    def name(self) -> str:
        return "file"
    
    @property
    def version(self) -> str:
        return "1.0.0"
    
    @property
    def description(self) -> str:
        return "读取本地文件内容"
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="read_file",
            description="读取本地文件内容",
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件路径"
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最大读取字符数，默认 5000"
                    }
                },
                "required": ["file_path"]
            }
        )
    
    def execute(self, params: dict) -> ToolResult:
        file_path = params.get("file_path")
        max_chars = params.get("max_chars", 5000)
        
        if not file_path:
            return ToolResult(success=False, error="缺少 file_path 参数")
        
        if not os.path.exists(file_path):
            return ToolResult(success=False, error=f"文件不存在: {file_path}")
        
        ext = Path(file_path).suffix.lower()
        
        # 根据文件类型处理
        if ext == ".pdf":
            return self._read_pdf(file_path, max_chars)
        elif ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"]:
            return self._read_image(file_path)
        elif ext == ".txt":
            return self._read_text(file_path, max_chars)
        else:
            # 默认尝试读取文本
            return self._read_text(file_path, max_chars)
    
    def _read_text(self, file_path: str, max_chars: int) -> ToolResult:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n\n[文件过长，已截断到前 {max_chars} 字符]"
            
            return ToolResult(
                success=True,
                data=f"文件: {os.path.basename(file_path)}\n大小: {len(content)} 字符\n\n内容:\n{content}"
            )
        except Exception as e:
            return ToolResult(success=False, error=f"读取失败: {e}")
    
    def _read_pdf(self, file_path: str, max_chars: int) -> ToolResult:
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(file_path)
            parts = []
            
            for i, page in enumerate(reader.pages[:20]):
                text = page.extract_text()
                if text and text.strip():
                    parts.append(f"--- 第{i+1}/{len(reader.pages)}页 ---\n{text}")
            
            content = "\n\n".join(parts)
            
            if not content.strip():
                return ToolResult(success=False, error="PDF 无法提取文字（可能是扫描件）")
            
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n\n[文件过长，已截断]"
            
            return ToolResult(
                success=True,
                data=f"PDF: {os.path.basename(file_path)} (共 {len(reader.pages)} 页)\n\n{content}"
            )
        except ImportError:
            return ToolResult(success=False, error="缺少 PyPDF2 库")
        except Exception as e:
            return ToolResult(success=False, error=f"PDF 读取失败: {e}")
    
    def _read_image(self, file_path: str) -> ToolResult:
        return ToolResult(
            success=False,
            error="图片识别需要视觉模型支持，请上传图片描述或使用支持视觉的 LLM"
        )
