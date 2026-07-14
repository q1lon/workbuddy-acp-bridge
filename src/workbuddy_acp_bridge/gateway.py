"""WorkBuddy Remote Gateway 的安全发现逻辑。

@author 李杰
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


MAX_SESSION_FILE_BYTES = 64 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_HEARTBEAT_AGE_SECONDS = 30.0
HOST_CWD_MARKER = "workbuddy-host-cli"


class GatewayDiscoveryError(RuntimeError):
    """表示无法发现可信的 WorkBuddy Gateway。"""


@dataclass(frozen=True, slots=True)
class GatewayEndpoint:
    """保存发现到的 Gateway 地址及非敏感会话元数据。"""

    base_url: str
    pid: int
    last_heartbeat: float
    session_file: Path
    session_id: str | None = None
    cwd: Path | None = None

    @property
    def is_host(self) -> bool:
        """判断该端点是否为 WorkBuddy 常驻 Host Runtime。"""

        return self.cwd is not None and HOST_CWD_MARKER in str(self.cwd).lower()

    @property
    def acp_url(self) -> str:
        """返回 ACP 主通道地址。"""

        return f"{self.base_url}/api/v1/acp"

    @property
    def connect_url(self) -> str:
        """返回 ACP 建连地址。"""

        return f"{self.base_url}/api/v1/acp/connect"


class GatewayDiscovery:
    """从 WorkBuddy 临时会话文件中发现当前 Gateway。"""

    def __init__(self, workbuddy_home: Path | None = None) -> None:
        """初始化发现器，但不读取任何文件。"""

        self.workbuddy_home = (workbuddy_home or self._default_home()).expanduser()

    def discover(self) -> GatewayEndpoint:
        """返回心跳时间最新的可信本机 Gateway。"""

        return self._latest(self._discover_candidates(), "没有发现可用的 WorkBuddy Gateway")

    def discover_host(self) -> GatewayEndpoint:
        """返回用于创建新 ACP 会话的常驻 Host Runtime。"""

        candidates = [item for item in self._discover_candidates() if item.is_host]
        return self._latest(candidates, "没有发现 WorkBuddy Host Runtime")

    def _discover_candidates(self) -> list[GatewayEndpoint]:
        """扫描并返回心跳仍新鲜的可信 Gateway 候选项。"""

        sessions_dir = self.workbuddy_home / "sessions"
        if not sessions_dir.is_dir():
            raise GatewayDiscoveryError(
                f"未找到 WorkBuddy 会话目录：{sessions_dir}，请先启动 WorkBuddy。"
            )

        candidates: list[GatewayEndpoint] = []
        for session_file in sessions_dir.glob("*.json"):
            candidate = self._read_candidate(session_file, sessions_dir)
            if candidate is not None and self._is_fresh(candidate):
                candidates.append(candidate)
        return candidates

    @staticmethod
    def _latest(candidates: list[GatewayEndpoint], message: str) -> GatewayEndpoint:
        """返回最新候选项，空集合转换为可读的发现异常。"""

        if not candidates:
            raise GatewayDiscoveryError(f"{message}；请确认 WorkBuddy 正在运行。")
        return max(candidates, key=lambda item: item.last_heartbeat)

    @staticmethod
    def _is_fresh(endpoint: GatewayEndpoint) -> bool:
        """按毫秒心跳过滤已退出进程遗留的会话文件。"""

        age_ms = time.time() * 1000 - endpoint.last_heartbeat
        return 0 <= age_ms <= MAX_HEARTBEAT_AGE_SECONDS * 1000

    @staticmethod
    def validate_base_url(endpoint: str) -> str:
        """校验并规范化只允许回环地址的 Gateway URL。"""

        if not isinstance(endpoint, str) or not endpoint.strip():
            raise GatewayDiscoveryError("Gateway endpoint 为空。")
        parsed = urlparse(endpoint.strip())
        # 桥接器持有临时会话令牌，因此绝不能把请求发送到远程主机。
        if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
            raise GatewayDiscoveryError("Gateway 仅允许使用本机 HTTP 回环地址。")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise GatewayDiscoveryError("Gateway URL 不能包含凭据、查询参数或片段。")
        try:
            port = parsed.port
        except ValueError as exc:
            raise GatewayDiscoveryError("Gateway 端口无效。") from exc
        if port is None or not 1 <= port <= 65535:
            raise GatewayDiscoveryError("Gateway 必须包含有效端口。")
        if parsed.path not in {"", "/"}:
            raise GatewayDiscoveryError("Gateway endpoint 必须是服务根地址。")
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        return f"http://{host}:{port}"

    def _read_candidate(
        self, session_file: Path, sessions_dir: Path
    ) -> GatewayEndpoint | None:
        """读取单个会话文件，非法或过期格式直接忽略。"""

        try:
            resolved_dir = sessions_dir.resolve(strict=True)
            resolved_file = session_file.resolve(strict=True)
            # 防止符号链接把扫描范围带出 WorkBuddy sessions 目录。
            if resolved_dir not in resolved_file.parents:
                return None
            if resolved_file.stat().st_size > MAX_SESSION_FILE_BYTES:
                return None
            data = json.loads(resolved_file.read_text(encoding="utf-8"))
            return self._candidate_from_data(data, resolved_file)
        except (OSError, UnicodeError, json.JSONDecodeError, GatewayDiscoveryError):
            return None

    def _candidate_from_data(
        self, data: Any, session_file: Path
    ) -> GatewayEndpoint | None:
        """把会话 JSON 转为经过验证的 Gateway 候选项。"""

        if not isinstance(data, dict):
            return None
        endpoint = data.get("endpoint")
        pid = data.get("pid")
        heartbeat = data.get("lastHeartbeat", 0)
        session_id = data.get("sessionId")
        cwd = data.get("cwd")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        if not isinstance(heartbeat, (int, float)) or isinstance(heartbeat, bool):
            return None
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 256
        ):
            return None
        if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
            return None
        return GatewayEndpoint(
            base_url=self.validate_base_url(endpoint),
            pid=pid,
            last_heartbeat=float(heartbeat),
            session_file=session_file,
            session_id=session_id.strip() if isinstance(session_id, str) else None,
            cwd=Path(cwd) if isinstance(cwd, str) else None,
        )

    @staticmethod
    def _default_home() -> Path:
        """按环境变量和用户目录推导 WorkBuddy 配置目录。"""

        configured = os.environ.get("WORKBUDDY_HOME")
        if configured:
            return Path(configured)
        return Path.home() / ".workbuddy"
