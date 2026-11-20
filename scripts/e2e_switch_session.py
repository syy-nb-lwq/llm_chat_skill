"""端到端：模拟前端的「新对话 / 切换会话」（不重连，只重发 init）。

跑之前后端要在 8000 上。这是**手动验证**用的，不进 pytest：
它打的是真实库和真实 WS 端点，跑一次会在库里留下两条空会话痕迹
（init 不建行，但 Session 对象留在内存里直到 TTL 回收）。
"""
import asyncio
import json
import urllib.request
import uuid

import websockets

BASE = "127.0.0.1:8000"
CONN = "e2e-" + uuid.uuid4().hex[:8]


def rpc(method, args):
    return json.dumps({"request": {"method": method, "arguments": args, "call_id": uuid.uuid4().hex}})


def http_json(path):
    with urllib.request.urlopen(f"http://{BASE}{path}", timeout=10) as r:
        return json.load(r)


def pick_existing_session():
    """挑一条真库里有消息的会话（不写死 id，换台机器也能跑）。"""
    for s in http_json("/api/sessions")["sessions"]:
        if s["message_count"] > 0:
            return s["session_id"]
    raise SystemExit("库里没有带消息的会话，先聊两句再跑")


async def wait_connected(ws, timeout=15):
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        req = msg.get("request") or {}
        if req.get("method") != "notify":
            continue  # response 之类，跳过
        # 必须 ACK，否则服务端 publish 卡住
        await ws.send(json.dumps({"response": {"result": None, "result_type": "NoneType", "call_id": req["call_id"]}}))
        blob = (req.get("arguments") or {}).get("data") or {}
        data = blob.get("data") or blob
        if data.get("event") == "connected":
            return data.get("payload") or {}


def messages_of(session_id):
    data = http_json(f"/api/session/messages?session_id={session_id}&limit=50")
    return data["count"]


async def main():
    existing = pick_existing_session()
    print("用来切换的已有会话:", existing)
    async with websockets.connect(f"ws://{BASE}/pubsub") as ws:
        await ws.send(rpc("subscribe", {"topics": [f"events/{CONN}"]}))
        await asyncio.sleep(0.6)

        # 1) 新对话：init 不带 session_id
        await ws.send(rpc("publish", {"topics": [f"init/{CONN}"], "data": {"client_id": CONN, "session_id": ""}, "sync": False}))
        first = await wait_connected(ws)
        print("1. 新会话签发:", first["session_id"])

        # 2) 切到已有会话
        await ws.send(rpc("publish", {"topics": [f"init/{CONN}"], "data": {"client_id": CONN, "session_id": existing}, "sync": False}))
        second = await wait_connected(ws)
        print("2. 切到已有:", second["session_id"], "| 库里消息数:", messages_of(second["session_id"]))
        assert second["session_id"] == existing, "没切过去"

        # 3) 再开一条新的
        await ws.send(rpc("publish", {"topics": [f"init/{CONN}"], "data": {"client_id": CONN, "session_id": ""}, "sync": False}))
        third = await wait_connected(ws)
        print("3. 再开新会话:", third["session_id"], "| 库里消息数:", messages_of(third["session_id"]))
        assert third["session_id"] not in (first["session_id"], second["session_id"])
        print("\nOK：同一连接换会话不用重连，session_id 每次都按预期更换")


asyncio.run(main())
