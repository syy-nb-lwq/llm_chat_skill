"""Soul - Agent 身份系统

参考 OpenClaw 的 SOUL.md 设计,用配置文件定义 Agent 的身份、性格和行为风格。
"""
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.logger import get_logger


@dataclass
class Soul:
    """Agent 身份定义"""
    name: str = "Agent"                                    # Agent 名字
    personality: str = "专业、高效、乐于助人"            # 性格描述
    communication_style: str = "简洁明了"                  # 沟通风格
    values: List[str] = field(default_factory=list)      # 核心价值观
    standing_instructions: List[str] = field(default_factory=list)  # 持续性指令
    expertise: List[str] = field(default_factory=list)   # 专业领域
    boundaries: List[str] = field(default_factory=list)  # 行为边界
    custom_rules: Dict[str, str] = field(default_factory=dict)  # 自定义规则
    metadata: Dict[str, Any] = field(default_factory=dict)  # 额外元数据

    def to_system_prompt(self) -> str:
        """转换为 system prompt"""
        lines = [
            f"# 身份",
            f"你是 {self.name}。",
            "",
            f"# 性格",
            f"{self.personality}",
            "",
        ]
        
        if self.values:
            lines.extend([
                "# 核心价值观",
                *[f"- {v}" for v in self.values],
                "",
            ])
        
        if self.expertise:
            lines.extend([
                "# 专业领域",
                *[f"- {e}" for e in self.expertise],
                "",
            ])
        
        if self.standing_instructions:
            lines.extend([
                "# 持续性指令",
                "你必须始终遵守以下规则:",
                *[f"- {inst}" for inst in self.standing_instructions],
                "",
            ])
        
        if self.communication_style:
            lines.extend([
                "# 沟通风格",
                f"{self.communication_style}",
                "",
            ])
        
        if self.boundaries:
            lines.extend([
                "# 行为边界",
                "你必须避免以下行为:",
                *[f"- {b}" for b in self.boundaries],
                "",
            ])
        
        if self.custom_rules:
            lines.extend([
                "# 自定义规则",
                *[f"- {k}: {v}" for k, v in self.custom_rules.items()],
                "",
            ])
        
        return "\n".join(lines)


class SoulLoader:
    """SOUL 配置加载器,支持热重载"""
    
    DEFAULT_SOUL = Soul()
    
    def __init__(self, soul_path: Optional[Path] = None):
        if soul_path is None:
            soul_path = Path(__file__).parent.parent / "soul" / "SOUL.md"
        self.soul_path = soul_path
        self.logger = get_logger()
        self._cached_soul: Optional[Soul] = None
        self._last_modified: float = 0
    
    def load(self, force_reload: bool = False) -> Soul:
        """加载 SOUL 配置
        
        Args:
            force_reload: 强制重新加载(忽略缓存)
        """
        # 检查文件是否存在
        if not self.soul_path.exists():
            self.logger.warning("SoulLoader", f"SOUL 文件不存在: {self.soul_path}, 使用默认配置")
            return self.DEFAULT_SOUL
        
        # 检查是否需要重新加载
        current_mtime = self.soul_path.stat().st_mtime
        if not force_reload and self._cached_soul and current_mtime == self._last_modified:
            return self._cached_soul
        
        # 解析 SOUL 文件
        try:
            soul = self._parse_soul_file(self.soul_path.read_text(encoding="utf-8"))
            self._cached_soul = soul
            self._last_modified = current_mtime
            self.logger.info("SoulLoader", f"加载 SOUL: {soul.name}")
            return soul
        except Exception as e:
            self.logger.error("SoulLoader", f"解析 SOUL 文件失败: {e}")
            return self._cached_soul or self.DEFAULT_SOUL
    
    def _parse_soul_file(self, content: str) -> Soul:
        """解析 SOUL 文件内容
        
        支持 Markdown + YAML front-matter 格式:
        ```markdown
        ---
        name: "小智"
        personality: "专业、高效"
        ---
        
        # 价值观
        - 用户隐私第一
        - 透明度优先
        ```
        """
        lines = content.strip().split("\n")
        
        # 初始化默认值
        soul = Soul()
        current_section = None
        section_content: List[str] = []
        
        # 检查是否有 front-matter
        if lines and lines[0].strip() == "---":
            # 提取 front-matter
            end_idx = None
            for i, line in enumerate(lines[1:], 1):
                if line.strip() == "---":
                    end_idx = i
                    break
            
            if end_idx:
                # 解析 YAML front-matter
                yaml_content = "\n".join(lines[1:end_idx])
                soul = self._parse_yaml_frontmatter(yaml_content)
                lines = lines[end_idx + 1:]
        
        # 解析 Markdown 内容
        for line in lines:
            stripped = line.strip()
            
            if not stripped or stripped.startswith("<!--"):
                continue
            
            # 检查是否是新章节
            if stripped.startswith("#"):
                # 保存上一个章节
                if current_section and section_content:
                    self._apply_section(soul, current_section, section_content)
                current_section = stripped.lstrip("#").strip().lower()
                section_content = []
            elif stripped.startswith("- ") or stripped.startswith("* "):
                section_content.append(stripped[2:])
            elif stripped.startswith("-"):
                section_content.append(stripped[1:].strip())
            elif section_content:
                # 多行内容,合并
                section_content[-1] += " " + stripped
        
        # 处理最后一个章节
        if current_section and section_content:
            self._apply_section(soul, current_section, section_content)
        
        return soul
    
    def _parse_yaml_frontmatter(self, yaml_content: str) -> Soul:
        """解析 YAML front-matter"""
        soul = Soul()
        
        for line in yaml_content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            
            if ": " in line:
                key, value = line.split(": ", 1)
                value = value.strip().strip('"').strip("'")
                
                if key == "name":
                    soul.name = value
                elif key == "personality":
                    soul.personality = value
                elif key == "communication_style":
                    soul.communication_style = value
        
        return soul
    
    def _apply_section(self, soul: Soul, section: str, content: List[str]) -> None:
        """应用章节内容"""
        if "价值观" in section or "value" in section:
            soul.values.extend(content)
        elif "指令" in section or "instruction" in section:
            soul.standing_instructions.extend(content)
        elif "专业" in section or "expertise" in section:
            soul.expertise.extend(content)
        elif "边界" in section or "boundary" in section:
            soul.boundaries.extend(content)
        elif "沟通" in section or "communication" in section:
            # 取第一条作为沟通风格
            if content:
                soul.communication_style = content[0]
        elif "性格" in section or "personality" in section:
            if content:
                soul.personality = content[0]
    
    def save(self, soul: Soul) -> None:
        """保存 SOUL 配置"""
        self.soul_path.parent.mkdir(parents=True, exist_ok=True)
        
        lines = [
            "---",
            f'name: "{soul.name}"',
            f'personality: "{soul.personality}"',
            f'communication_style: "{soul.communication_style}"',
            "---",
            "",
        ]
        
        if soul.values:
            lines.extend(["# 核心价值观", ""])
            lines.extend([f"- {v}" for v in soul.values])
            lines.append("")
        
        if soul.expertise:
            lines.extend(["# 专业领域", ""])
            lines.extend([f"- {e}" for e in soul.expertise])
            lines.append("")
        
        if soul.standing_instructions:
            lines.extend(["# 持续性指令", ""])
            lines.extend([f"- {inst}" for inst in soul.standing_instructions])
            lines.append("")
        
        if soul.boundaries:
            lines.extend(["# 行为边界", ""])
            lines.extend([f"- {b}" for b in soul.boundaries])
            lines.append("")
        
        self.soul_path.write_text("\n".join(lines), encoding="utf-8")
        
        # 更新缓存
        self._cached_soul = soul
        self._last_modified = self.soul_path.stat().st_mtime
        
        self.logger.info("SoulLoader", f"保存 SOUL: {soul.name}")


# ---- 全局单例 ----
_soul_loader: Optional[SoulLoader] = None


def get_soul_loader() -> SoulLoader:
    """获取 SoulLoader 全局实例"""
    global _soul_loader
    if _soul_loader is None:
        _soul_loader = SoulLoader()
    return _soul_loader


def reset_soul_loader() -> None:
    """重置 SoulLoader"""
    global _soul_loader
    _soul_loader = None
