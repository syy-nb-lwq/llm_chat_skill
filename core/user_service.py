"""UserService —— 假用户服务(无登录体系下的用户身份分发)。

背景:
    项目目前没有登录体系,但长期记忆必须按用户隔离(否则 A 的记忆会被 B 召回)。
    这里用一个"假用户服务"补齐身份:服务端签发用户凭证(user_token),
    客户端持久化后每次连接都带上,服务端据此认出同一个用户。

职责:
    - 签发 / 解析用户令牌
    - 维护用户档案(id / 租户 / 显示名 / 设置)
    - 列出用户(用于测试隔离与运维)

不做的事:
    - 不做密码、验证码、OAuth 等真实认证
    - 令牌默认不过期(单机个人环境);需要时可加 exp 字段
"""
from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.auth import issue_user_token, verify_user_token
from infra.logger import get_logger


DEFAULT_TENANT = "default"


@dataclass
class User:
    """用户档案。"""

    id: str
    tenant_id: str = DEFAULT_TENANT
    display_name: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_seen_at: str = field(default_factory=lambda: datetime.now().isoformat())
    settings: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return asdict(self)


class UserService:
    """用户服务:用户档案的增删查 + 令牌签发/解析。

    存储:``memory/users/<user_id>.json``,便于人工查看和迁移。
    """

    def __init__(self, base_path: Optional[Path] = None):
        if base_path is None:
            base_path = Path(__file__).parent.parent / "memory" / "users"
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.logger = get_logger()

    # ===== 令牌 =====

    def issue_token(self, user_id: str, tenant_id: str = DEFAULT_TENANT) -> str:
        """签发用户令牌。"""
        return issue_user_token(user_id, tenant_id)

    def resolve_token(self, token: str) -> Optional[User]:
        """用令牌换回用户档案。令牌无效或用户不存在 → None。"""
        if not token:
            return None
        payload = verify_user_token(token)
        if not payload:
            return None
        user = self.get(payload["user_id"])
        if user is None:
            return None
        # 令牌里的租户必须与档案一致,防止令牌被改写
        if payload.get("tenant_id") and payload["tenant_id"] != user.tenant_id:
            self.logger.warning("UserService", f"令牌租户不匹配: {user.id}")
            return None
        return user

    # ===== 档案 =====

    def _path(self, user_id: str) -> Path:
        return self.base_path / f"{user_id}.json"

    def get(self, user_id: str) -> Optional[User]:
        path = self._path(user_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.warning("UserService", f"读取用户档案失败 {path}: {exc}")
            return None
        return User(
            id=data.get("id", user_id),
            tenant_id=data.get("tenant_id", DEFAULT_TENANT),
            display_name=data.get("display_name", ""),
            created_at=data.get("created_at", ""),
            last_seen_at=data.get("last_seen_at", ""),
            settings=data.get("settings", {}) or {},
        )

    def _save(self, user: User) -> None:
        path = self._path(user.id)
        try:
            # 自愈:目录可能被外部删了(日志清理/部署脚本)。
            # 不建目录的话,每次写档案都失败,用户会变成"每次都是新用户" ——
            # 表现和"历史对话丢了"一模一样。
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(user.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            self.logger.error("UserService", f"写用户档案失败 {path}: {exc}")

    def create(
        self,
        display_name: str = "",
        tenant_id: str = DEFAULT_TENANT,
    ) -> User:
        """新建用户并返回档案。"""
        user = User(
            id=f"u-{int(datetime.now().timestamp() * 1000)}-{secrets.token_hex(4)}",
            tenant_id=tenant_id or DEFAULT_TENANT,
            display_name=display_name or "未命名用户",
        )
        self._save(user)
        self.logger.info("UserService", f"创建用户: {user.id}")
        return user

    def get_or_create(
        self,
        token: str = "",
        display_name: str = "",
        tenant_id: str = DEFAULT_TENANT,
    ) -> User:
        """按令牌取用户;令牌无效/用户不存在则新建。

        这是"假用户服务"的核心入口:调用方只需把客户端带来的 token 传进来,
        就能得到一个稳定的用户身份。
        """
        if token:
            user = self.resolve_token(token)
            if user is not None:
                self.touch(user.id)
                return user
            self.logger.info("UserService", "令牌无效或用户不存在,新建用户")
        return self.create(display_name=display_name, tenant_id=tenant_id)

    def touch(self, user_id: str) -> None:
        """更新最后活跃时间。"""
        user = self.get(user_id)
        if user is None:
            return
        user.last_seen_at = datetime.now().isoformat()
        self._save(user)

    def update_settings(self, user_id: str, settings: Dict[str, Any]) -> bool:
        user = self.get(user_id)
        if user is None:
            return False
        user.settings.update(settings or {})
        self._save(user)
        return True

    def list_users(self) -> List[User]:
        users: List[User] = []
        for path in sorted(self.base_path.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            users.append(User(
                id=data.get("id", path.stem),
                tenant_id=data.get("tenant_id", DEFAULT_TENANT),
                display_name=data.get("display_name", ""),
                created_at=data.get("created_at", ""),
                last_seen_at=data.get("last_seen_at", ""),
                settings=data.get("settings", {}) or {},
            ))
        return users

    def delete(self, user_id: str) -> bool:
        path = self._path(user_id)
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except Exception as exc:
            self.logger.error("UserService", f"删除用户失败 {user_id}: {exc}")
            return False


# ===== 单例 =====

_user_service: Optional[UserService] = None


def get_user_service() -> UserService:
    global _user_service
    if _user_service is None:
        _user_service = UserService()
    return _user_service


def reset_user_service() -> None:
    global _user_service
    _user_service = None
