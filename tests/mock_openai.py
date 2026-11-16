"""本地 OpenAI 兼容 mock —— 让"真 LLM 通道"能被真正跑起来。

## 为什么需要它

`tests/test_intent_eval.py::test_l1_llm_channel` 需要真实 API key,
所以它长期是 **skip** 状态 —— 也就是说,**这条通道的代码路径从未被执行过**。
后果:某天改了模型名、API 参数形状或解析逻辑,CI 全绿,
只有等拿到 key 跑 nightly 才发现坏了。

这个 mock 让同一段代码在**没有 key 的情况下**也能跑:
真的起 HTTP 服务 → 真的走 OpenAI SDK → 真的解析响应 → 真的算指标。

它验证的是**管线通不通**,不是"模型准不准"。
准确率仍然需要真实模型(那件事没法用替身代替)。

## 用法

```python
from tests.mock_openai import MockOpenAI

def responder(messages, payload):
    # messages: [{"role": ..., "content": ...}]
    return '{"label": "task", "confidence": 0.9}'

with MockOpenAI(responder) as mock:
    # mock.base_url → http://127.0.0.1:<port>/v1
    ...
```
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, List, Optional


Responder = Callable[[List[dict], dict], str]


class _Handler(BaseHTTPRequestHandler):
    # 用 HTTP/1.0(默认):每个响应后连接关闭。
    # 1.1 的 keep-alive 要求每个响应都有准确的 Content-Length,
    # SSE 那种"读到 [DONE] 为止"的响应给不出长度,客户端会一直等。

    # 外部注入
    responder: Responder = None
    recorder: List[dict] = None
    chunk_size: int = 12

    def log_message(self, *args):  # 静音
        pass

    def do_GET(self):
        body = json.dumps(
            {"requests": self.recorder}, ensure_ascii=False
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}

        messages = payload.get("messages") or []
        self.recorder.append({"messages": messages, "payload": payload})

        try:
            text = self.responder(messages, payload)
        except Exception as exc:  # 让服务端把错误暴露出来,而不是静默返回空
            import traceback

            traceback.print_exc()
            text = f'{{"error": "responder raised: {exc}"}}'

        if payload.get("stream"):
            self._send_sse(text, payload)
            return

        body = json.dumps(
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": 0,
                "model": payload.get("model", "mock"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, text: str, payload: dict):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        step = max(1, self.chunk_size)
        for i in range(0, len(text), step):
            chunk = {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": payload.get("model", "mock"),
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": text[i:i + step]},
                        "finish_reason": None,
                    }
                ],
            }
            self.wfile.write(
                f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
            )
            self.wfile.flush()

        done = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": payload.get("model", "mock"),
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        self.wfile.write(f"data: {json.dumps(done, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class MockOpenAI:
    """起一个本地 OpenAI 兼容服务的上下文管理器。

    Attributes:
        base_url: 指向 mock 的 OpenAI base_url(含 `/v1`)
        requests: 收到的所有请求(可断言"确实发出去了")
    """

    def __init__(self, responder: Responder, chunk_size: int = 12):
        self.responder = responder
        self.chunk_size = chunk_size
        self.requests: List[dict] = []
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._saved_no_proxy: Optional[dict] = None

    # ---- 生命周期 ----

    def __enter__(self) -> "MockOpenAI":
        # **把 localhost 排除在系统代理之外。**
        #
        # 踩过的坑:Windows 的 Internet Settings 里配了代理时,
        # httpx(`trust_env` 默认 True)会通过 `getproxies()` 从**注册表**读到它 ——
        # 环境变量里看不到 HTTP_PROXY,但请求照样被丢给代理,
        # 于是发往 127.0.0.1 的 mock 请求收到 502 Bad Gateway。
        # 症状:L1 调用全部超时/失败,测试红得莫名其妙,而手工 curl 又是通的。
        # 测试必须与宿主机代理配置无关。
        self._saved_no_proxy = {
            k: os.environ.get(k) for k in ("NO_PROXY", "no_proxy")
        }
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        os.environ["no_proxy"] = "127.0.0.1,localhost"

        handler = type(
            "_BoundHandler",
            (_Handler,),
            {
                "responder": staticmethod(self.responder),
                "recorder": self.requests,
                "chunk_size": self.chunk_size,
            },
        )
        self._server = HTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        for k, v in (self._saved_no_proxy or {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False

    @property
    def base_url(self) -> str:
        assert self._server is not None, "MockOpenAI 还没启动"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    # ---- 辅助 ----

    @staticmethod
    def extract_user_input(messages: List[dict]) -> str:
        """从 L1 提示词里抠出被分类的原始输入。

        L1 的 user 消息以 `请分类下面这句话:` 结尾(见 `core/intent_labels.py`),
        这里按这个约定还原;取不到就退回最后一条 user 消息。
        """
        for msg in reversed(messages or []):
            if msg.get("role") != "user":
                continue
            content = msg.get("content") or ""
            marker = "请分类下面这句话:"
            if marker in content:
                return content.split(marker, 1)[1].strip()
            return content.strip()
        return ""
