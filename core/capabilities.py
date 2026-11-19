"""能力清单 —— 让 agent 知道自己**真的**能做什么。

## 这个模块为什么存在

实机踩过：用户问「你能做什么？能不能执行代码、发网络请求、读写文件？」
agent 回答：

> 在当前对话环境中，我没有实际执行能力，因此不能直接执行代码、
> 不能直接发起网络请求、不能直接读写文件。

**这全是错的。** 系统里明明有 `python_exec`（子进程执行，能发 HTTP、能读写
工作区文件）、`web_search` / `web_fetch`（联网）、`read_file`（读文件）、
`read_document`（读文档）、`create_tool`（自己造工具）。

## 根因

回答生成层（`OrchestratorAgent`）的 system prompt 只有一句话：
「你是一个专业的回答生成助手」。

它**不知道自己兜里有什么工具**，只看得到这一轮的 `tool_results`。
当规划层判定「这是个问题,不需要工具」（`tool_tasks=[]`）时，
回答层拿到的是空数据 → 退回基础模型的通用自我认知 →
而通用模型对自己的默认认知就是「我只是个文本模型」。

所以它不是在撒谎，是**真的不知道自己有什么**。

## 修法

把「能力清单」注入回答生成的 system prompt，和用户画像是同一个位置
（见 `docs/14` §2.1：常驻约束放 system，不放 user）。

**关键：清单必须从 ToolHub 动态生成，不能写死。**
写死的话，关掉 `CODE_EXEC_ENABLED` 之后它会继续吹牛说能执行代码 ——
那是另一种错误，而且更难发现。

## 预算

和画像一样按字符数卡（默认 900）。工具多的时候只列分类和代表性工具，
不逐个罗列 —— 目的是让模型知道"有这能力"，不是给它一份工具手册。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from infra.logger import get_logger


#: 注入的能力块最大字符数
CAPABILITY_BLOCK_MAX_CHARS = 900

#: 能力分类：(判据工具名, 能力描述)
#: 判据工具"存在"才写进清单 —— 这样关掉开关后清单会跟着变
_CAPABILITY_RULES: List[Dict[str, Any]] = [
    {
        "tools": ("python_exec",),
        "text": "执行 Python 代码（独立子进程）：能计算、处理数据、调用外部 HTTP 接口、"
                "在工作区读写文件",
    },
    {
        "tools": ("web_search", "web_fetch"),
        "text": "联网：搜索公开信息、抓取网页正文",
    },
    {
        "tools": ("read_file",),
        "text": "读文件：能直接读本机/工作区里的文本文件（源码、配置、日志），"
                "单个文件 10MB 以内",
    },
    {
        "tools": ("read_document", "locate_document"),
        "text": "读文档（PDF/Word/Excel/PPT/Markdown）：能提取内容并引用原文位置",
    },
    {
        "tools": ("write_script", "list_scripts"),
        "text": "写脚本并保存成文件复用",
    },
    {
        "tools": ("create_tool",),
        "text": "**自己造新工具**：能给一个 REST 接口写声明式工具并立即使用，"
                "也能把脚本包装成工具（写操作/脚本类需人工批准）",
    },
]

#: 与工具无关、由系统固定提供的能力
_ALWAYS_AVAILABLE = (
    "生成的文件（报表/图片/视频等）会作为产物返回，用户能在界面上预览和下载",
    "长期记忆：会记住用户稳定的偏好与习惯，并在之后的回答里遵守",
)


def build_capability_block(max_chars: int = CAPABILITY_BLOCK_MAX_CHARS) -> str:
    """从**当前实际注册的工具**生成能力清单。

    返回空串表示拿不到工具信息（此时不注入，保持原行为）。
    """
    try:
        from tools.hub import get_tool_hub

        hub = get_tool_hub()
        names = set(hub.names())
    except Exception as exc:
        try:
            get_logger().warning("Capabilities", f"读取工具清单失败: {exc}")
        except Exception:
            pass
        return ""

    if not names:
        return ""

    lines: List[str] = []
    for rule in _CAPABILITY_RULES:
        if any(t in names for t in rule["tools"]):
            lines.append(f"- {rule['text']}")

    for text in _ALWAYS_AVAILABLE:
        lines.append(f"- {text}")

    # MCP 外部工具（数量 + 来源）
    try:
        status = hub.get_source_status()
        mcp_tools = sum(
            s.get("tool_count", 0)
            for s in (status or {}).values()
            if s.get("type") == "mcp" and s.get("state") == "connected"
        )
        if mcp_tools:
            lines.append(f"- 已接入 {mcp_tools} 个外部 MCP 工具（文件读写、网页抓取等）")
    except Exception:
        pass

    if not lines:
        return ""

    block = (
        "## 你实际具备的能力（来自当前运行环境，以此为准）\n"
        + "\n".join(lines)
        + f"\n\n当前共注册 {len(names)} 个工具。"
    )

    if len(block) > max_chars:
        block = block[: max_chars - 12].rstrip() + "\n…（已截断）"

    block += (
        "\n\n**回答关于自身能力的问题时，以上面的清单为准。**"
        "不要声称自己没有执行能力 —— 上面列出的能力是真的可以用的。"
        "确实做不到的事（比如需要用户自己的账号密钥、需要访问用户本机未授权的路径），"
        "要具体说明缺什么，而不是笼统地说「我做不到」。"
    )
    return block
