"""C-01 管理接口鉴权测试。

覆盖:
- 未配置 OWNER_TOKEN 时,管理接口保持开放(单机个人环境默认行为)
- 配置 OWNER_TOKEN 后:缺 token → 401,token 错误 → 403,token 正确 → 放行
- 只读接口不受鉴权影响
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def client():
    from backend.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_management_api_open_when_owner_token_not_configured(client, monkeypatch):
    from infra.config import config

    monkeypatch.setattr(config, "owner_token", "")
    # 未启用鉴权 → 请求应走到业务逻辑(技能不存在 → 404),而不是 401/403
    response = client.delete("/api/skills/__nonexistent_skill__")
    assert response.status_code == 404


def test_management_api_requires_correct_token(client, monkeypatch):
    from infra.config import config

    monkeypatch.setattr(config, "owner_token", "secret-token")

    # 缺 token
    assert client.delete("/api/skills/__nonexistent_skill__").status_code == 401

    # token 错误
    response = client.delete(
        "/api/skills/__nonexistent_skill__",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 403

    # token 正确 → 放行到业务逻辑
    response = client.delete(
        "/api/skills/__nonexistent_skill__",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 404


def test_post_endpoints_are_protected(client, monkeypatch):
    from infra.config import config

    monkeypatch.setattr(config, "owner_token", "secret-token")

    response = client.post("/api/skills/reload")
    assert response.status_code == 401

    response = client.post(
        "/api/skills/reload",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 200


def test_readonly_endpoints_stay_open(client, monkeypatch):
    from infra.config import config

    monkeypatch.setattr(config, "owner_token", "secret-token")
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/features").status_code == 200
    assert client.get("/api/skills").status_code == 200
    assert client.get("/api/tools").status_code == 200


def test_get_owner_token_normalizes_blank_value(monkeypatch):
    from infra.auth import get_owner_token
    from infra.config import config

    monkeypatch.setattr(config, "owner_token", "")
    assert get_owner_token() is None

    monkeypatch.setattr(config, "owner_token", "   ")
    assert get_owner_token() is None

    monkeypatch.setattr(config, "owner_token", "abc")
    assert get_owner_token() == "abc"
