"""BaseAgent - 所有 Agent 的基类,统一 LLM 调用 / Trace / JSON 重试"""
import json
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from infra.llm import get_llm_client
from infra.logger import get_logger, LogType


class BaseAgent(ABC):
    """所有 Agent 的基类。

    提供:
    - 统一的 LLM 调用入口 `think()` / `think_json()`
    - JSON Schema 校验 + 自动重试
    - 自动关联 trace_id
    """

    name: str = "BaseAgent"

    def __init__(self):
        self.llm = get_llm_client()
        self.logger = get_logger()

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
        """单轮 LLM 调用。

        - 若提供 output_schema: 自动 JSON 解析 + 校验,失败重试
        - 每次调用都记录 LLM_REQUEST / LLM_RESPONSE 日志
        """
        messages = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": user_prompt},
        ]
        last_err = None
        for attempt in range(1, retries + 1):
            self.logger.info(
                LogType.LLM_REQUEST, self.name,
                f"LLM request (attempt {attempt}/{retries})",
                {"messages_preview": _preview_messages(messages), "schema": output_schema},
            )
            try:
                raw = await self.llm.chat_with_retry(messages, temperature=temperature)
            except Exception as e:
                last_err = str(e)
                self.logger.error(LogType.LLM_ERROR, self.name, f"LLM 调用异常: {e}")
                if attempt >= retries:
                    raise
                continue

            self.logger.info(
                LogType.LLM_RESPONSE, self.name,
                "LLM response", {"raw_preview": raw[:500]},
            )
            if output_schema is None:
                return raw
            parsed = _try_parse_json(raw, output_schema)
            if parsed is not None:
                return parsed
            last_err = "JSON 解析或校验失败"
            self.logger.warning(
                LogType.LLM_ERROR, self.name,
                f"{last_err},重试 ({attempt}/{retries})",
            )
        raise ValueError(f"{self.name}: 重试 {retries} 次仍无法产出符合 schema 的输出: {last_err}")

    async def think_json(self, user_prompt: str, schema: Dict) -> Dict:
        return await self.think(user_prompt, output_schema=schema)


def _preview_messages(messages: List[Dict]) -> List[Dict]:
    """给日志用的简化预览"""
    return [{"role": m["role"], "preview": (m["content"] or "")[:120]} for m in messages]


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
    # 轻量校验:仅检查必填字段,不引入 jsonschema 依赖
    required = schema.get("required", [])
    if not all(_has_key(obj, k) for k in required):
        return None
    # 枚举校验(简单实现)
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