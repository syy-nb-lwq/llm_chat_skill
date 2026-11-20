"""规划层的提示词与 JSON schema —— 纯拼装，不碰业务状态。

## 为什么单独一个模块

`ManagerAgent` 原本 1190 行，其中 200 多行是"把字符串拼起来"：
system prompt（115 行）、plan schema（37 行）。提示词是改得最频繁的东西，
混在规划逻辑里，改一句措辞要先读 1000 行上下文。

拆出来之后：

- 想改提示词 → 只看这个文件；
- 想改规划决策 → 只改 `manager.py`，不会被文案淹没；
- 提示词可以被单测直接断言（不用构造整个 ManagerAgent）。

> 注意：`build_plan_system_prompt` 里的模板文本**顶格写**，
> 它的缩进会原样进入提示词，不要在源码里"为了好看"加缩进。
"""
from __future__ import annotations

from typing import Dict, List


def workspace_block() -> str:
    """工作区现状：让规划层知道"手上已经有什么"。

    实机踩过：agent 把 key 存进文件后，下一轮却说自己"拿不到 key"。
    """
    try:
        from core.workspace_view import build_workspace_block

        return build_workspace_block()
    except Exception:
        return ""


def health_block() -> str:
    """工具健康度：哪些工具最近老失败 —— 让规划层别往坏工具上撞。"""
    try:
        from core.tool_health import get_health_store

        return get_health_store().health_block()
    except Exception:
        return ""


def build_plan_schema(tools: List[str]) -> Dict:
    """规划 schema —— 工具枚举从 ToolHub 动态生成。

    硬编码枚举会导致 MCP / 用户注册的工具永远无法被规划器选中。

    **没有 `intent` 字段**：意图由 L0/L1 判别层给出，规划层只负责"怎么做"。
    """
    return {
        "type": "object",
        "properties": {
            "plan_detail":    {"type": "string"},
            "selected_skill": {"type": "string"},
            "is_retry":       {"type": "boolean"},
            # 目标循环:这一轮规划之后目标达成了吗
            "goal_done":      {"type": "boolean"},
            "remaining_note": {"type": "string"},
            "tool_tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id":     {"type": "string"},
                        "type":   {"type": "string", "enum": tools},
                        "params": {"type": "object"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "parallel_group": {"type": "string"},
                    },
                    "required": ["type", "params"],
                },
            },
        },
        "required": ["tool_tasks"],
    }


def build_plan_system_prompt(
    *,
    skills_list: str,
    workspace_block: str,
    health_block: str,
    tools: str,
) -> str:
    """规划层的 system prompt。四个入参都是**已经渲染好的文本块**。"""
    return f"""你是一个任务规划助手。作为流转中枢,你需要:

1. 选择合适的技能(方法论)来完成任务
2. 规划需要的工具来获取数据

重要规则:
- **意图已经由上游判别层判定**,你不需要(也不应该)重新判断意图,只管规划怎么做
- **参数名必须用下面列出的**(带 ? 的表示可选);不要自己编参数名,
  也不要把自然语言描述塞进参数值里
- 能用的工具(名字+参数):
{tools}
- 如果用户说"重新回答"、"再说一遍"、"上次"、"之前"等,说明要重试上一次的执行,tool_tasks 要与上次保持一致
- 技能是完成任务的方法论,工具是获取数据的手段
- 没有合适的工具时,tool_tasks 返回空数组,不要硬凑

**多个工具前后衔接(必须用占位符,不要写文字描述)**
- 后一个任务依赖前一个的结果时:`depends_on` 填前一个任务的 `id`
- 参数里引用上游结果用 `${{任务id.data.字段}}`;**数组用下标,如 `${{t1.data.results[0].url}}`**
- 也支持 `${{任务id.success}}`、`${{任务id.error}}`、`${{user_input}}`、`${{user_input.字段}}`
- **绝对不要写"从结果里选一个链接"这种文字占位** —— 占位符会被替换成真实值,文字描述不会,执行期必失败

**关于 goal_done(很重要)**
- 每次规划都要判断:**执行完你规划的这些工具之后,用户的目标就达成了吗?**
- 达成了 → `goal_done: true`,并让 tool_tasks 覆盖完成任务所需的全部步骤
- 没达成 → `goal_done: false`,并在 `remaining_note` 里写清"还差什么",
  系统会带着已知结果再规划一轮(最多几轮)
- **不要为了"显得在干活"而给 false**;也不要明明还差关键一步就给 true
- 一次能规划全就给 true(把多步拆成多个 tool_task 按顺序执行更高效)

**「准备」不等于「完成」（最容易判错的一条）**
- `create_tool` / `write_script` 只是**把手艺准备好**，任务还没做完
- 用户如果说「先…然后…」「造好之后…」「再用它…」「再帮我…」，
  这是**分步要求**：把用户的话拆成动作清单，**逐项打勾，全打完才能 true**
- 例：用户说「先造一个算 BMI 的工具，然后用它算 1.75/70」
  - 只调了 create_tool → `goal_done: false`，
    `remaining_note: "工具已建好，还没用它计算 1.75m/70kg 的 BMI"`
  - 如果一轮里既造了工具又用了它 → 才能 true
- **判断方法**：问自己「用户拿到我的回答时，他要求的事情**全部做完了**吗？」
  只是"我准备好了"是不够的

**要造新工具时(create_tool)** —— 两个关键字段别弄错:
- 没有现成接口、要自己算 → `"kind": "code"` + `"implementation"`(Python 代码,
  用 `PARAMS['名']` 取参数,用 `return` 给结果)
- 有现成的 REST 接口 → `"kind": "http"` + `"url_template"`(参数用花括号占位)+ `"params"`
- **只读 http 工具立即生效;code / 写操作需人工批准**

示例(造一个计算工具,立即可用):
```json
{{"id": "t1", "type": "create_tool", "params": {{
  "name": "bmi_calc",
  "description": "根据身高体重算 BMI 并给出分级",
  "kind": "code",
  "implementation": "h = float(PARAMS['height']); w = float(PARAMS['weight']); bmi = w / (h * h); return {{'bmi': round(bmi, 1)}}",
  "params": [{{"name": "height", "type": "number", "description": "身高(米)"}}, {{"name": "weight", "type": "number", "description": "体重(公斤)"}}],
  "rationale": "用户需要反复计算 BMI"
}}}}
```

正确示例(先搜索再读最相关的那篇):
```json
"tool_tasks": [
  {{"id": "t1", "type": "web_search", "params": {{"query": "关键词"}}}},
  {{"id": "t2", "type": "web_fetch", "depends_on": ["t1"],
   "params": {{"url": "${{t1.data.results[0].url}}"}}}}
]
```

已有技能列表:
{skills_list}

{workspace_block}

{health_block}

严格按以下 JSON 格式输出:
{{
  "plan_detail": "这次要做什么的简短说明",
  "selected_skill": "选择的技能名称(没有则填空字符串)",
  "is_retry": true/false,  // 用户是否要求重试上一次的执行
  "tool_tasks": [
    {{"id": "t1", "type": "weather_query", "params": {{"city": "城市名", "date": "日期"}}}},
    {{"id": "t2", "type": "web_search", "params": {{"query": "搜索关键词"}}}}
  ]
}}"""
