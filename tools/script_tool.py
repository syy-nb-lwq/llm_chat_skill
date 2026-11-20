"""脚本类工具 —— 把代码/脚本包装成可复用的工具。

## 两种来源

- `kind=code`：提案里带**内联代码**（agent 最自然的产出形态）
- `kind=script`：指向工作区里已保存的脚本（`write_script` 存的）

两者执行方式一样，都走 `python_exec` 的沙箱：
独立临时目录 + `-I` 隔离 + env 白名单（拿不到 API key）+ 超时 + AST 黑名单。

## 参数怎么进来

执行时把参数写成 `input.json` 放进沙箱，脚本里通过全局 **`PARAMS`** (dict) 读：

```python
city = PARAMS.get("city", "北京")
```

（不用命令行参数：`python_exec` 的启动方式不支持 argv；
而且 JSON 注入能保留类型 —— 数字还是数字，布尔还是布尔。）

## 为什么执行前要"包一层"

agent 写出来的代码十有八九长这样：

```python
def run(owner, repo):
    ...
    return {"stars": 123}
```

直接当模块 body 跑会有两个问题：

1. **顶层 `return` 是 SyntaxError**（`return` 只在函数里合法）
2. `run` **定义了但没人调用**，等于什么都没做

所以这里按形态分两种模式（用 AST 判，不靠正则猜）：

| 形态 | 怎么跑 |
|---|---|
| 有顶层 `return` | 整体缩进进 `__tool_main__()`，用返回值当结果 |
| 没有顶层 `return` | 直接当模块跑（这样 `def run` 落在全局），完了自动调用 `run`/`main`/`execute` |

**注意：生成的运行器本身也要过 `python_exec` 的静态检查**，
所以不能用 `import sys`（在黑名单里）、也不能用 `exec`。
出错就用 `raise` / `traceback`。
"""
from __future__ import annotations

import ast
import json
from typing import Any, Dict, List

from infra.logger import get_logger
from tools.base import Tool, ToolParam, ToolResult, ToolSchema


#: 公共前置：读参数 + 结果输出工具函数
_PREAMBLE = '''\
import json as _json
import traceback as _tb

try:
    PARAMS = _json.load(open('input.json', encoding='utf-8'))
except Exception:
    PARAMS = {}


def __emit_result(value):
    """把结果打到 stdout —— python_exec 只回 stdout/stderr。"""
    if value is None:
        return
    try:
        print(_json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        print(str(value))
'''

#: 形态二：没有顶层 return —— 当模块跑，完了自动调用 run/main/execute
_FOOTER_AUTOCALL = '''
_result = None
for _fn_name in ("run", "main", "execute"):
    _fn = globals().get(_fn_name)
    if not callable(_fn):
        continue
    try:
        _result = _fn(**PARAMS)
        break
    except TypeError:
        continue
    except Exception:
        _tb.print_exc()
        raise

__emit_result(_result)
'''

#: 形态一：有顶层 return —— 包进函数，用返回值
_FUNC_TEMPLATE = '''
def __tool_main__():
{body}

__emit_result(__tool_main__())
'''


def _has_top_level_return(code: str) -> bool:
    """判断代码里有没有**模块级**的 `return`。

    只看模块顶层作用域 —— 函数/类里面的 `return` 是合法的，不用管。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    stack: List[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue                      # 跳过子作用域
        if isinstance(node, ast.Return):
            return True
        for child in ast.iter_child_nodes(node):
            stack.append(child)
    return False


def build_runner_code(user_code: str) -> str:
    """把用户代码包成能真正跑起来的脚本。"""
    code = (user_code or "").strip("\n")
    if not code.strip():
        code = "pass"

    if _has_top_level_return(code):
        body = "\n".join(
            ("    " + ln) if ln.strip() else "" for ln in code.splitlines()
        )
        return _PREAMBLE + _FUNC_TEMPLATE.format(body=body or "    pass")

    return _PREAMBLE + "\n" + code + "\n" + _FOOTER_AUTOCALL


class ScriptTool(Tool):
    """把一个脚本/代码包装成工具。"""

    def __init__(self, proposal):
        self.proposal = proposal
        self.name = proposal.name
        self.description = proposal.description
        self.logger = get_logger()

    def schema(self) -> ToolSchema:
        params: List[ToolParam] = []
        for p in self.proposal.params or []:
            if not isinstance(p, dict):
                continue
            params.append(
                ToolParam(
                    name=str(p.get("name") or ""),
                    type=str(p.get("type") or "string"),
                    description=str(p.get("description") or ""),
                    required=bool(p.get("required", True)),
                    default=p.get("default"),
                    enum=p.get("enum"),
                )
            )
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=params,
            returns={"stdout": "脚本输出（结果通常是 JSON）", "artifacts": "生成的文件"},
        )

    async def execute(self, **kwargs) -> ToolResult:
        from tools.python_exec import PythonExecTool, _resolve_workspace_script

        # 取用户代码：内联优先，否则读工作区脚本
        user_code = (getattr(self.proposal, "implementation", "") or "").strip()
        rel = (self.proposal.script_path or "").strip()
        if not user_code:
            target = _resolve_workspace_script(rel)
            if target is None:
                return ToolResult(success=False, error=f"脚本不存在或不在工作区内：{rel!r}")
            try:
                user_code = target.read_text(encoding="utf-8")
            except Exception as exc:
                return ToolResult(success=False, error=f"读取脚本失败：{exc}")

        # 参数补齐默认值
        values: Dict[str, Any] = {}
        for p in self.proposal.params or []:
            if not isinstance(p, dict):
                continue
            pname = str(p.get("name") or "")
            if not pname:
                continue
            if pname in kwargs and kwargs[pname] is not None:
                values[pname] = kwargs[pname]
            elif p.get("default") is not None:
                values[pname] = p.get("default")

        executor = PythonExecTool(timeout_s=self._timeout())
        result = await executor.execute(
            code=build_runner_code(user_code),
            files=[{
                "name": "input.json",
                "content": json.dumps(values, ensure_ascii=False),
            }],
        )

        if not result.success:
            return ToolResult(
                success=False,
                error=result.error or "脚本执行失败",
                data=result.data,
                meta={"tool": self.name},
            )

        data = result.data or {}
        out: Dict[str, Any] = {
            "stdout": data.get("stdout", ""),
            "stderr": data.get("stderr", ""),
            "returncode": data.get("returncode", 0),
        }
        # 结果通常是 JSON —— 尽量解析出来，让 agent 拿到结构化数据
        parsed = self._try_parse(out["stdout"])
        if parsed is not None:
            out["result"] = parsed
        if data.get("artifacts"):
            out["artifacts"] = data["artifacts"]
        return ToolResult(success=True, data=out, meta={"tool": self.name})

    @staticmethod
    def _try_parse(text: str):
        """从 stdout 里找一段 JSON 当结果（找不到就算了）。"""
        blob = (text or "").strip()
        if not blob:
            return None
        try:
            return json.loads(blob)
        except Exception:
            pass
        # 多行输出时，取最后一个非空行试试
        for line in reversed(blob.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except Exception:
                continue
        return None

    def _timeout(self) -> int:
        t = getattr(self.proposal, "timeout_s", None)
        try:
            return int(t) if t else 60
        except Exception:
            return 60
