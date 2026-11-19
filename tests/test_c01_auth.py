"""管理接口在单用户模式下的行为。

本地单用户：没有 OWNER_TOKEN / 鉴权。管理接口（删除技能、审批补丁等）
直接开放；只读接口同样开放。
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


def test_management_api_is_open(client):
    """本地单用户：管理接口无需令牌，请求直接走到业务逻辑。"""
    # 技能不存在 → 404，而不是 401/403
    response = client.delete("/api/skills/__nonexistent_skill__")
    assert response.status_code == 404


def test_post_endpoints_are_open(client):
    """本地单用户：无需令牌即可调用。"""
    response = client.post("/api/skills/reload")
    assert response.status_code == 200


def test_readonly_endpoints_stay_open(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/features").status_code == 200
    assert client.get("/api/skills").status_code == 200
    assert client.get("/api/tools").status_code == 200
