"""「刷新/重启后历史对话没了」的根因与回归测试。

## 实机现象

用户刷新页面 → 历史对话消失。

日志证据:

    UserService: 令牌无效或用户不存在,新建用户
    Session: 会话 s-xxx 不属于该用户,新建会话

## 三个根因(都修了)

1. **`SESSION_SECRET` 没配** → 令牌签名密钥每次进程启动都是随机值 →
   客户端 localStorage 里的 `user_token` 全部失效 → 服务端把你当成**新用户**
   → 建**新会话** → 自然没有历史。
   这不是"刷新丢数据",是**身份丢了**。

2. **`SessionMemoryStore._connect` 不建目录** → `memory/` 被外部删掉后,
   每次读会话都 `unable to open database file` → 接口 500(而不是"没有历史")。

3. **`UserService._save` 不建目录** → 写用户档案失败 → 用户变成"每次都新建"。

## 教训

会话内容一直是全量保留的。丢的从来不是数据,是**"你是同一个人"这件事**。
"""
import json
import time

import pytest


# ============================================================
# 1. 令牌密钥必须跨重启稳定
# ============================================================

def test_secret_is_stable_when_configured(monkeypatch):
    """配了 SESSION_SECRET → 两次取到的密钥必须完全一致(跨重启也一样)。"""
    from infra import auth

    monkeypatch.setattr(auth.config, "session_secret", "fixed-secret-for-test")
    a = auth._get_session_secret()
    b = auth._get_session_secret()
    assert a == b
    assert a == b"fixed-secret-for-test"


def test_tokens_survive_process_restart_when_configured(monkeypatch):
    """**核心**:配了固定密钥,"重启后"的令牌仍然能验过。

    模拟重启:把进程级缓存的随机密钥清掉(等价于新进程)。
    只要 SESSION_SECRET 配了,签出来的令牌就不受进程影响。
    """
    from infra import auth

    monkeypatch.setattr(auth.config, "session_secret", "fixed-secret-for-test")

    # 进程 1 签发
    token = auth.issue_user_token("u-42")
    assert auth.verify_user_token(token)["user_id"] == "u-42"

    # 模拟进程重启:清掉进程级密钥缓存
    monkeypatch.setattr(auth, "_process_secret", None)

    # 进程 2 仍然验得过
    payload = auth.verify_user_token(token)
    assert payload is not None, "重启后令牌失效了 —— 用户会被当成新用户,表现为历史丢失"
    assert payload["user_id"] == "u-42"


def test_tokens_die_on_restart_without_secret(monkeypatch):
    """反过来:没配密钥时,重启确实会让令牌失效(这就是那个 bug)。

    这条测试是"记录现状",不是在夸它 —— 提醒后来人为什么必须配。
    """
    from infra import auth

    monkeypatch.setattr(auth.config, "session_secret", "")
    monkeypatch.setattr(auth, "_process_secret", None)
    token = auth.issue_user_token("u-42")

    # 模拟重启
    monkeypatch.setattr(auth, "_process_secret", None)

    assert auth.verify_user_token(token) is None, (
        "没配 SESSION_SECRET 时重启应导致令牌失效 —— 这正是用户报的"
        "「历史没了」的机制"
    )


def test_warn_if_unstable_secret(monkeypatch):
    from infra import auth

    monkeypatch.setattr(auth.config, "session_secret", "fixed")
    assert auth.warn_if_unstable_secret() is True

    monkeypatch.setattr(auth.config, "session_secret", "")
    assert auth.warn_if_unstable_secret() is False


# ============================================================
# 2. 存储自愈
# ============================================================

def test_session_store_heals_after_dir_deleted(tmp_path):
    """`memory/` 被删掉后,读会话不能 500。"""
    import shutil

    from core.session_memory import SessionMemoryStore

    store = SessionMemoryStore(db_path=tmp_path / "mem" / "sessions.db")
    store.append_message("s1", "u1", "user", "hi")

    # 删掉整个目录(模拟外部清理)
    shutil.rmtree(tmp_path / "mem", ignore_errors=True)

    # 读不能抛 —— 目录没了要能自愈(重建目录 + 补建表)
    store.get_messages("s1")

    # 文件真被删掉了 → 数据也该没了(store 会重建一个空库)。
    # 注意:store 现在**复用连接**,Windows 上文件被我们持着句柄,
    # 外部删除会被系统挡掉 —— 这时文件还在,数据也还在,不是 bug。
    db_file = tmp_path / "mem" / "sessions.db"
    if not db_file.exists():
        assert store.session_exists("s1") is False
        assert store.get_messages("s1") == []


def test_session_store_rebuilds_after_db_file_removed(tmp_path):
    """DB 文件被删掉后,store 会重建 —— 读到的是空的,不是旧数据。

    这里先 ``close()`` 再删,是为了让删除在所有平台上都能真正发生
    (Windows 上文件被连接持着句柄时删不掉)。等价于"进程重启后
    目录被清理掉了"的真实场景。
    """
    from core.session_memory import SessionMemoryStore

    store = SessionMemoryStore(db_path=tmp_path / "mem" / "sessions.db")
    store.append_message("s1", "u1", "user", "hi")
    assert store.get_messages("s1")  # 先确认写进去了

    store.close()
    (tmp_path / "mem" / "sessions.db").unlink()

    # 同一个 store 实例:文件没了 → 自动重建 → 读到的是空的
    assert store.session_exists("s1") is False
    assert store.get_messages("s1") == []

    # 重建之后还能正常写
    store.append_message("s2", "u1", "user", "重建后写入")
    assert len(store.get_messages("s2")) == 1


def test_session_store_can_write_after_dir_deleted(tmp_path):
    import shutil

    from core.session_memory import SessionMemoryStore

    store = SessionMemoryStore(db_path=tmp_path / "mem" / "sessions.db")
    shutil.rmtree(tmp_path / "mem", ignore_errors=True)

    store.append_message("s1", "u1", "user", "恢复后写入")
    msgs = store.get_messages("s1")
    assert len(msgs) == 1
    assert msgs[0].content == "恢复后写入"


def test_user_service_heals_after_dir_deleted(tmp_path):
    """`memory/users/` 被删掉后,写档案不能失败。

    失败了会怎样:用户令牌查不到 → 每次都当新用户 → 历史全丢。
    """
    import shutil

    from core.user_service import UserService

    svc = UserService(base_path=tmp_path / "users")
    user = svc.create()
    assert svc.get(user.id) is not None

    shutil.rmtree(tmp_path / "users", ignore_errors=True)

    user2 = svc.create()
    assert svc.get(user2.id) is not None, "写档案失败了 —— 用户每次都会被当成新的"


# ============================================================
# 3. 端到端:会话能按令牌取回来
# ============================================================

def test_history_roundtrip_through_api(tmp_path, monkeypatch):
    """存消息 → 用令牌取历史。这是前端刷新后做的事。"""
    from core import session_memory as sm
    from core.session_memory import SessionMemoryStore
    from infra.auth import issue_session_token

    store = SessionMemoryStore(db_path=tmp_path / "sessions.db")
    monkeypatch.setattr(sm, "get_session_memory_store", lambda: store)

    store.append_message("s-e2e", "u-e2e", "user", "记住数字 42")
    store.append_message("s-e2e", "u-e2e", "assistant", "好的")

    from backend.main import app
    from fastapi.testclient import TestClient

    token = issue_session_token("u-e2e", "s-e2e", "c-e2e")
    with TestClient(app) as c:
        resp = c.get(
            "/api/session/messages", headers={"Authorization": f"Bearer {token}"}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "s-e2e"
    assert data["count"] == 2
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]
