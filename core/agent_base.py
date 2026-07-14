"""BaseAgent - 所有 Agent 的基类,统一 LLM 调用"""
import json
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from infra.llm import get_llm_client
from infra.logger import get_logger


# Feature Flag
def _soul_enabled() -> bool:
    try:
        from infra.config import config
        return bool(getattr(config, 'soul_enabled', False))
    except Exception:
        return False


class BaseAgent(ABC):
    """所有 Agent 的基类"""

    name: str = "BaseAgent"

    def __init__(self):
        self.llm = get_llm_client()
        self.logger = get_logger()
        self._soul_loader = None

    @property
    def soul_loader(self):
        """延迟获取 SoulLoader"""
        if self._soul_loader is None and _soul_enabled():
            try:
                from core.soul import get_soul_loader
                self._soul_loader = get_soul_loader()
            except Exception:
                pass
        return self._soul_loader

    def get_soul_prompt(self) -> str:
        """获取 Soul system prompt"""
        loader = self.soul_loader
        if loader is None:
            return ""
        try:
            soul = loader.load()
            return soul.to_system_prompt()
        except Exception:
            return ""

    @abstractmethod
    def system_prompt(self) -> str: ...

    async def think(
        self,
        user_prompt: str,
        *,
        output_schema: Optional[Dict] = None,
        temperature: Optional[float] = None,
        retries: int = 3,
    ) -> Any:
        """单轮 LLM 调用"""
        # 组装 messages
        messages = []
        
        # 添加 Soul prompt (如果启用)
        soul_prompt = self.get_soul_prompt()
        if soul_prompt:
            messages.append({"role": "system", "content": soul_prompt})
        
        # 添加 Agent 自己的 system prompt
        system = self.system_prompt()
        if system:
            if messages and messages[0]["role"] == "system":
                # 合并 Soul 和 system prompt
                messages[0]["content"] = messages[0]["content"] + "\n\n" + system
            else:
                messages.append({"role": "system", "content": system})
        
        messages.append({"role": "user", "content": user_prompt})
        last_err = None
        for attempt in range(1, retries + 1):
            self.logger.info(self.name, f"LLM 请求 (尝试 {attempt}/{retries})")
            try:
                raw = await self.llm.chat_with_retry(messages, temperature=temperature)
            except Exception as e:
                last_err = str(e)
                self.logger.error(self.name, f"LLM 调用失败: {e}")
                if attempt >= retries:
                    raise
                continue

            self.logger.info(self.name, f"LLM 响应: {raw[:100]}...")
            if output_schema is None:
                return raw
            parsed = _try_parse_json(raw, output_schema)
            if parsed is not None:
                return parsed
            last_err = "JSON 解析失败"
            self.logger.warning(self.name, f"{last_err},重试 ({attempt}/{retries})")
        raise ValueError(f"{self.name}: 重试 {retries} 次仍无法产出符合 schema 的输出: {last_err}")

    async def think_json(self, user_prompt: str, schema: Dict) -> Dict:
        return await self.think(user_prompt, output_schema=schema)


_JSON_RE = re.compile(r"\{[\s\S]*\}")


def _try_parse_json(raw: str, schema: Optional[Dict]) -> Optional[Dict]:
    """尝试提取 JSON 并(可选)按 schema 校验"""
    if not raw:
        return None
    m = _JSON_RE.search(raw)
    if not m:
        return None
    try:
        obj = json.loads(m.group())
    except Exception:
        return None
    if schema is None:
        return obj
    required = schema.get("required", [])
    if not all(_has_key(obj, k) for k in required):
        return None
    props = schema.get("properties", {})
    for k, spec in props.items():
        if k not in obj:
            continue
        if "enum" in spec and obj[k] not in spec["enum"]:
            return None
    return obj


def _has_key(obj: Any, dotted_key: str) -> bool:
    cur = obj
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True
