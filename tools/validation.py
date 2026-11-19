"""工具参数校验与类型转换。

## 为什么单独一层

`tools/hub.py` 是所有工具调用的唯一入口，但过去它把参数**原样**展开：
`execute(**params)`。于是同一个错误在两类工具上表现完全不同：

- 强签名工具 → `TypeError`，至少报错
- `**kwargs` 工具 → `kwargs.get("path")` 拿到 `None` → 返回"path 不能为空"，
  更糟的是有些工具会给默认值 → **返回成功，但什么都没干**

后者最危险：模型会把这个"成功"当事实继续推理。

## 这里的职责

1. **必填**：缺了直接报错
2. **未知**：拼错的参数名直接报错，并给出最接近的候选（`file_path` → `path`）
3. **类型**：按 schema 做保守转换（`"5"` → `5`、`"true"` → `True`、JSON 字符串 → list/dict）

转换只做**语义等价**的那一档：`"5"` 转 5 可以，`"abc"` 转 5 不行 —— 后者报错，
而不是悄悄用默认值。宁可失败，也不能让工具带着错误参数"成功"。
"""
from __future__ import annotations

import difflib
import json
from typing import Any, Dict, List, Optional, Tuple

from tools.base import ToolParam, ToolSchema

#: schema 里可能出现的类型别名（LLM 生成的工具常写 Python 类型名）
_TYPE_ALIASES = {
    "str": "string",
    "text": "string",
    "int": "integer",
    "long": "integer",
    "float": "number",
    "double": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
    "map": "object",
}

_TRUE_WORDS = {"true", "1", "yes", "y", "on", "是"}
_FALSE_WORDS = {"false", "0", "no", "n", "off", "否", ""}


def normalize_type(raw: str) -> str:
    t = (raw or "string").strip().lower()
    return _TYPE_ALIASES.get(t, t)


# ===== schema 归一化 =====


def _coerce_schema(schema: Any) -> Optional[ToolSchema]:
    """把 schema 归一到 `ToolSchema`。

    来源有三种：工具自己返回的 `ToolSchema`、工具上的 dict 属性、
    MCP 风格的 JSON Schema。拿不到可识别的结构时返回 None（= 不校验）。
    """
    if schema is None:
        return None
    if isinstance(schema, ToolSchema):
        return schema
    if not isinstance(schema, dict):
        return None

    # JSON Schema 风格：{"properties": {...}, "required": [...]}
    if "properties" in schema:
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        params = [
            ToolParam(
                name=str(name),
                type=str((info or {}).get("type") or "string"),
                description=str((info or {}).get("description") or ""),
                required=name in required,
            )
            for name, info in props.items()
        ]
        return ToolSchema(
            name=str(schema.get("name") or ""),
            description=str(schema.get("description") or ""),
            params=params,
        )

    # 项目风格：{"params": [{"name": ...}, ...]}
    raw_params = schema.get("params")
    if isinstance(raw_params, list):
        params = []
        for p in raw_params:
            if isinstance(p, ToolParam):
                params.append(p)
            elif isinstance(p, dict) and p.get("name"):
                params.append(
                    ToolParam(
                        name=str(p["name"]),
                        type=str(p.get("type") or "string"),
                        description=str(p.get("description") or ""),
                        required=bool(p.get("required", True)),
                        default=p.get("default"),
                        enum=p.get("enum"),
                    )
                )
        return ToolSchema(
            name=str(schema.get("name") or ""),
            description=str(schema.get("description") or ""),
            params=params,
        )

    return None


# ===== 单值转换 =====


def _to_string(value: Any) -> Tuple[Any, str]:
    if isinstance(value, str):
        return value, ""
    # bool 转 "True"/"False" 只会制造歧义，明确拒绝
    if isinstance(value, bool):
        return None, "需要字符串，收到布尔值"
    if isinstance(value, (int, float)):
        return str(value), ""
    return None, f"需要字符串，收到 {type(value).__name__}"


def _to_integer(value: Any) -> Tuple[Any, str]:
    if isinstance(value, bool):
        return None, "需要整数，收到布尔值"
    if isinstance(value, int):
        return value, ""
    if isinstance(value, float):
        if value.is_integer():
            return int(value), ""
        return None, f"需要整数，收到小数 {value}"
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text), ""
        except ValueError:
            try:
                f = float(text)
            except ValueError:
                return None, f"需要整数，收到 {value!r}"
            if f.is_integer():
                return int(f), ""
        return None, f"需要整数，收到 {value!r}"
    return None, f"需要整数，收到 {type(value).__name__}"


def _to_number(value: Any) -> Tuple[Any, str]:
    if isinstance(value, bool):
        return None, "需要数字，收到布尔值"
    if isinstance(value, (int, float)):
        return value, ""
    if isinstance(value, str):
        try:
            f = float(value.strip())
        except ValueError:
            return None, f"需要数字，收到 {value!r}"
        return (int(f) if f.is_integer() else f), ""
    return None, f"需要数字，收到 {type(value).__name__}"


def _to_boolean(value: Any) -> Tuple[Any, str]:
    if isinstance(value, bool):
        return value, ""
    if isinstance(value, (int, float)):
        if value in (0, 1):
            return bool(value), ""
        return None, f"需要布尔值，收到 {value!r}"
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_WORDS:
            return True, ""
        if text in _FALSE_WORDS:
            return False, ""
        return None, f"需要布尔值，收到 {value!r}"
    return None, f"需要布尔值，收到 {type(value).__name__}"


def _to_array(value: Any) -> Tuple[Any, str]:
    if isinstance(value, list):
        return value, ""
    if isinstance(value, tuple):
        return list(value), ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return [], ""
        try:
            parsed = json.loads(text)
        except ValueError:
            return None, "需要数组，字符串不是合法 JSON"
        if isinstance(parsed, list):
            return parsed, ""
        return None, "需要数组，JSON 解析结果不是数组"
    return None, f"需要数组，收到 {type(value).__name__}"


def _to_object(value: Any) -> Tuple[Any, str]:
    if isinstance(value, dict):
        return value, ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}, ""
        try:
            parsed = json.loads(text)
        except ValueError:
            return None, "需要对象，字符串不是合法 JSON"
        if isinstance(parsed, dict):
            return parsed, ""
        return None, "需要对象，JSON 解析结果不是对象"
    return None, f"需要对象，收到 {type(value).__name__}"


_COERCERS = {
    "string": _to_string,
    "integer": _to_integer,
    "number": _to_number,
    "boolean": _to_boolean,
    "array": _to_array,
    "object": _to_object,
}


def coerce_value(param: ToolParam, value: Any) -> Tuple[Any, str]:
    """按 schema 类型转换单个值。返回 (转换后的值, 错误信息)。"""
    target = normalize_type(param.type)
    coercer = _COERCERS.get(target)
    if coercer is None:
        # 未知类型（any / 自定义）不做转换，也不拦 —— 留给工具自己处理
        return value, ""
    return coercer(value)


# ===== 整体校验 =====


def _unknown_message(name: str, allowed: List[str]) -> str:
    """未知参数的错误信息 —— 带上最接近的候选，直接告诉模型该改哪个字。"""
    hint = ""
    close = difflib.get_close_matches(name, allowed, n=1, cutoff=0.6)
    if close:
        hint = f"；是不是想写 {close[0]}?"
    return f"未知参数 {name}{hint}（可用参数：{', '.join(allowed) or '无'}）"


def validate_params(
    schema: Any,
    params: Dict[str, Any],
    *,
    strict_unknown: bool = True,
) -> Tuple[bool, Dict[str, Any], str]:
    """按 schema 校验并转换参数。

    返回 `(ok, 转换后的参数, 错误信息)`。**不修改传入的 dict。**

    `schema` 为 None（拿不到契约）时直接放行 —— 没有契约就没法判断对错，
    此时拦下来只会把"能用"变成"不能用"。
    """
    normalized = _coerce_schema(schema)
    if normalized is None:
        return True, dict(params or {}), ""

    declared = {p.name: p for p in normalized.params if p.name}
    incoming = dict(params or {})
    out: Dict[str, Any] = {}
    errors: List[str] = []

    # 1) 未知参数
    if strict_unknown:
        allowed = list(declared.keys())
        for name in incoming:
            if name not in declared:
                errors.append(_unknown_message(str(name), allowed))

    # 2) 逐个字段：必填 / 类型
    for name, param in declared.items():
        if name not in incoming:
            if param.required:
                errors.append(f"缺少必填参数 {name}（{param.type}）")
            continue

        value = incoming[name]
        if value is None:
            if param.required:
                errors.append(f"必填参数 {name} 不能为空")
            # 可选参数给 None = 用工具默认值，直接丢掉这个键
            continue

        coerced, err = coerce_value(param, value)
        if err:
            errors.append(f"参数 {name}：{err}")
            continue
        out[name] = coerced

    # 3) enum 只在类型正确后校验
    for name, param in declared.items():
        if not param.enum or name not in out:
            continue
        if out[name] not in param.enum:
            errors.append(f"参数 {name} 取值非法：{out[name]!r}（可选：{param.enum}）")

    if errors:
        return False, out, "；".join(errors)
    return True, out, ""
