"""端到端：上传一个文档 → 用附件消息提问 → 看 Agent 是否真的读了它。

这是"上传入口"这件事的验收线：
  上传接口返回 200 只证明文件落地了，**不证明 Agent 用得上**。
真正要验证的是最后一步 —— 回答里出现只有文档里才有的内容。

跑之前后端要在 8000 上。手动验证用，不进 pytest（打真实库 + 真实 LLM）。
"""
import asyncio
import json
import urllib.request
import uuid
from pathlib import Path

import websockets

BASE = "127.0.0.1:8000"
CONN = "e2e-up-" + uuid.uuid4().hex[:8]

#: 只有读过文档才知道的数字 —— 回答里出现它，就证明确实读到了内容
SECRET_NUMBER = "1280"


def rpc(method, args):
    return json.dumps(
        {"request": {"method": method, "arguments": args, "call_id": uuid.uuid4().hex}}
    )


def make_docx() -> Path:
    from docx import Document

    path = Path("/tmp/e2e-上传验证.docx")
    doc = Document()
    doc.add_heading("Q3 经营简报", level=1)
    doc.add_paragraph(f"本季度营收 {SECRET_NUMBER} 万元，同比增长 23%。")
    doc.add_paragraph("主要增长来自跨境电商业务线，环比提升 8 个百分点。")
    doc.save(path)
    return path


def upload(path: Path, session_id: str) -> dict:
    """手工拼 multipart —— 不为了一个脚本引第三方依赖。"""
    boundary = "----" + uuid.uuid4().hex
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="session_id"\r\n\r\n'
        f"{session_id}\r\n"
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
    ).encode()
    body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"http://{BASE}/api/upload",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


async def wait_connected(ws, timeout=15):
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        req = msg.get("request") or {}
        if req.get("method") != "notify":
            continue
        await ws.send(
            json.dumps(
                {
                    "response": {
                        "result": None,
                        "result_type": "NoneType",
                        "call_id": req["call_id"],
                    }
                }
            )
        )
        blob = (req.get("arguments") or {}).get("data") or {}
        data = blob.get("data") or blob
        if data.get("event") == "connected":
            return data.get("payload") or {}


async def run_turn(ws, content: str, timeout=180):
    """发一条消息，收集事件直到 message_final。返回 (回答全文, 调用过的工具名)。"""
    await ws.send(rpc("publish", {"topics": [f"chat/{CONN}"], "data": {"content": content}, "sync": False}))

    text_parts = []
    tools = []
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        req = msg.get("request") or {}
        if req.get("method") != "notify":
            continue
        await ws.send(
            json.dumps(
                {
                    "response": {
                        "result": None,
                        "result_type": "NoneType",
                        "call_id": req["call_id"],
                    }
                }
            )
        )
        blob = (req.get("arguments") or {}).get("data") or {}
        data = blob.get("data") or blob
        event = data.get("event")
        payload = data.get("payload") or {}

        if event in ("message_delta", "message_replace"):
            piece = payload.get("content") or payload.get("delta") or ""
            if event == "message_replace":
                text_parts = [piece]
            else:
                text_parts.append(piece)
        elif event == "tool_call":
            tools.append(payload.get("tool") or payload.get("name") or "?")
        elif event == "message_final":
            piece = payload.get("content")
            if piece:
                text_parts = [piece]
            return "".join(text_parts), tools
        elif event == "error":
            return f"[错误] {payload.get('message')}", tools


async def main():
    doc = make_docx()
    print(f"1. 生成文档: {doc.name} ({doc.stat().st_size} B)")

    async with websockets.connect(f"ws://{BASE}/pubsub") as ws:
        await ws.send(rpc("subscribe", {"topics": [f"events/{CONN}"]}))
        await asyncio.sleep(0.6)

        await ws.send(
            rpc(
                "publish",
                {"topics": [f"init/{CONN}"], "data": {"client_id": CONN, "session_id": ""}, "sync": False},
            )
        )
        identity = await wait_connected(ws)
        session_id = identity["session_id"]
        print(f"2. 新会话: {session_id}")

        info = upload(doc, session_id)
        print(f"3. 上传成功: {info['path']}")
        assert info["readable"], f"上传后判定不可读: {info['hint']}"

        # 这正是前端 composeContent 拼出来的形状
        content = (
            "这份简报讲了什么？只给结论，一两句话。\n\n"
            f"【已上传文件】\n- {info['name']} → {info['path']}"
        )
        print("4. 提问（带附件段）…")
        answer, tools = await run_turn(ws, content)

        print(f"\n调用的工具: {tools or '（无）'}")
        print(f"回答:\n{answer[:600]}\n")

        assert "read_document" in tools, f"Agent 没有读文档，实际调用了: {tools}"
        assert SECRET_NUMBER in answer, "回答里没有文档中的数字，说明没读到内容"
        print(f"OK：文档被真的读了，回答里出现了只有原文才有的 {SECRET_NUMBER}")


asyncio.run(main())
