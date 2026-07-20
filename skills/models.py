"""技能数据模型"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Literal
import uuid


@dataclass
class SkillStep:
    """技能的一个可执行步骤"""
    id: str                                   # 唯一 id(同 skill 内)
    name: str = ""
    description: str = ""
    tool: Optional[str] = None                # 关联的工具名
    input_schema: Dict = field(default_factory=dict)  # JSON Schema,只用于校验/提示
    params: Dict = field(default_factory=dict)        # 实际传给工具的参数(支持 ${user_input.x} 和 ${step.data.x} 占位符)
    output_schema: Dict = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)   # 上游 step id
    parallel_group: Optional[str] = None      # 同组可并行执行
    template: Optional[str] = None            # 输出模板(可选)
    fallback: Optional[str] = None            # 失败时跳到的 step id
    retry: int = 0
    timeout_s: int = 30

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tool": self.tool,
            "params": self.params,
            "depends_on": self.depends_on,
            "parallel_group": self.parallel_group,
            "fallback": self.fallback,
            "retry": self.retry,
            "timeout_s": self.timeout_s,
        }


@dataclass
class Skill:
    """技能 = 方法论 + 步骤(DAG)"""
    name: str
    version: str = "1.0.0"
    capability: str = ""                      # 能力描述
    method: str = ""                         # 总方法论(给 Orchestrator)
    patterns: List[str] = field(default_factory=list)    # 触发关键词
    tags: List[str] = field(default_factory=list)
    steps: List[SkillStep] = field(default_factory=list)
    examples: List[str] = field(default_factory=list)

    # 元数据
    id: str = field(default_factory=lambda: f"skill_{uuid.uuid4().hex[:8]}")
    source: Literal["builtin", "taught", "imported"] = "builtin"
    author: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # 兼容旧字段(过渡期使用)
    legacy_steps_text: List[str] = field(default_factory=list)

    def has_structured_steps(self) -> bool:
        """是否声明了结构化步骤(DAG 可执行)"""
        return any(s.tool is not None for s in self.steps)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "capability": self.capability,
            "method": self.method,
            "patterns": self.patterns,
            "tags": self.tags,
            "steps": [s.to_dict() for s in self.steps],
            "examples": self.examples,
            "source": self.source,
            "author": self.author,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active": bool(getattr(self, "active", False)),
        }