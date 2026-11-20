"""回答流过滤器 —— 挡住模型幻觉的工具调用标记。

实机事故（2026-11-20 报销表加行）：模型把 <tool_call>```python ...
```</tool_call> 当成正文输出，用户看到半截代码而不是文件。
prompt 已禁止，这里是流式输出层的第二道防线。

核心难点：token 是逐块推的，<tool_call> 会被切成 <tool + _call>，
逐块独立判断会漏掉已经推出的前缀。所以过滤器必须跨 token 有状态。
"""
from core.answer_filter import ToolCallFilter, filter_answer


def feed_all(tokens):
    f = ToolCallFilter()
    out = []
    for t in tokens:
        out.extend(f.feed(t))
    out.extend(f.flush())
    return "".join(out)


# ---------- 一次性清洗 ----------

def test_removes_complete_tool_call_block():
    text = "我来帮你加一行。<tool_call>```python\nprint('x')\n```</tool_call>以上就是结果。"
    out = filter_answer(text)
    assert "我来帮你加一行。" in out
    assert "以上就是结果。" in out
    assert "<tool_call>" not in out
    assert "print('x')" not in out


def test_removes_unclosed_tool_call_to_end():
    """模型写到一半没闭标签 —— 从标记开始全部丢弃。"""
    out = filter_answer("正常开头。<tool_call>半截代码没有闭合")
    assert out == "正常开头。"


def test_removes_function_tag():
    out = filter_answer("答案<function>xxx</function>结束")
    assert out == "答案结束"


def test_keeps_normal_text():
    text = "这是一段普通回答，包含 < 和 > 符号，还有 **加粗** 和列表。"
    assert filter_answer(text) == text


def test_keeps_python_code_block():
    """用户要看的代码块（```python）应保留 —— 只过滤工具调用标记。"""
    code_answer = "示例：\n```python\nprint('hello')\n```\n希望有帮助。"
    assert filter_answer(code_answer) == code_answer


# ---------- 跨 token ----------

def test_tag_split_across_tokens():
    """标记被切成多个 token 也要识别。"""
    tokens = ["正常", "<tool", "_call>", "坏内容", "</tool", "_call>", "收尾"]
    out = feed_all(tokens)
    assert out == "正常收尾"


def test_angle_bracket_not_false_positive():
    """单独的 < 或 <too 不该被 hold 住吞掉。"""
    tokens = ["a < b", " and x <toolx> y"]
    out = feed_all(tokens)
    assert out == "a < b and x <toolx> y"


def test_flush_releases_partial_prefix():
    """流结束时 pending 里不是标记的内容要放出去。"""
    f = ToolCallFilter()
    out = list(f.feed("结尾<tool")) + list(f.flush())
    assert "".join(out) == "结尾<tool"


def test_char_by_char_stream():
    """最坏情况：每个 token 只有一个字符。"""
    text = "前<tool_call>丢弃</tool_call>后"
    out = feed_all(list(text))
    assert out == "前后"
