"""配置"""
import os
from dataclasses import dataclass


@dataclass
class Config:
    """配置"""
    # LLM
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4"
    
    # 路径
    skills_path: str = "skills"
    memory_path: str = "memory"
    vector_path: str = "vector_store"
    
    # Agent
    max_iterations: int = 10
    temperature: float = 0.7
    
    @classmethod
    def from_env(cls) -> 'Config':
        """从环境变量加载"""
        return cls(
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
            llm_model=os.getenv("LLM_MODEL", "gpt-4"),
            skills_path=os.getenv("SKILLS_PATH", "skills"),
            memory_path=os.getenv("MEMORY_PATH", "memory"),
            vector_path=os.getenv("VECTOR_PATH", "vector_store"),
            max_iterations=int(os.getenv("MAX_ITERATIONS", "10")),
            temperature=float(os.getenv("TEMPERATURE", "0.7")),
        )
    
    def validate(self) -> bool:
        """验证配置"""
        if not self.llm_api_key:
            print("错误: 请设置 LLM_API_KEY 环境变量")
            return False
        return True


# 全局配置
config = Config.from_env()
