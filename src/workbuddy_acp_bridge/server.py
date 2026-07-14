"""面向 Codex CLI 的 WorkBuddy MCP stdio 服务。

@author 李杰
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, BinaryIO

from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .acp_client import (
    DEFAULT_MODEL_ID,
    DEFAULT_THOUGHT_LEVEL,
    MODEL_ID_PATTERN,
    AcpError,
    AcpRunResult,
    AcpSessionUnavailableError,
    PermissionMode,
    ThoughtLevel,
    WorkBuddyAcpClient,
)
from .gateway import GatewayDiscovery, GatewayDiscoveryError, GatewayEndpoint


SERVER_NAME = "workbuddy-acp-bridge"
SERVER_VERSION = "0.1.0"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"
DEFAULT_SESSION_STATE_PATH = Path.home() / ".workbuddy" / "codex-acp-bridge-session.json"
MAX_CONFIG_FILE_BYTES = 64 * 1024
server = Server(SERVER_NAME)


def _load_default_model_id(config_path: Path | None = None) -> str:
    """从项目配置读取默认模型；文件或字段未配置时返回内置默认值。"""

    path = config_path or DEFAULT_CONFIG_PATH
    # 配置文件是可选项，首次安装或删除配置文件后交由 WorkBuddy 自动选择模型。
    if not path.is_file():
        return DEFAULT_MODEL_ID

    try:
        # 限量读取可以避免异常配置文件占用过多内存，同时不依赖读取前的文件大小检查。
        with path.open("rb") as config_file:
            raw_config = config_file.read(MAX_CONFIG_FILE_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"读取配置文件失败：{path}。") from exc
    # 超出上限通常意味着误选了文件，拒绝继续解析以便尽早暴露配置错误。
    if len(raw_config) > MAX_CONFIG_FILE_BYTES:
        raise ValueError(f"配置文件不能超过 {MAX_CONFIG_FILE_BYTES} 字节：{path}。")

    try:
        config = json.loads(raw_config.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"配置文件必须是有效的 UTF-8 JSON：{path}。") from exc
    # 根节点固定为对象，避免数组等结构导致配置字段语义不明确。
    if not isinstance(config, dict):
        raise ValueError(f"配置文件根节点必须是 JSON 对象：{path}。")

    configured_model_id = config.get("model_id")
    # 字段缺失、null 或空字符串都表示未配置，统一交由 WorkBuddy 自动选择模型。
    if configured_model_id is None:
        return DEFAULT_MODEL_ID
    if not isinstance(configured_model_id, str):
        raise ValueError("配置项 model_id 必须是字符串。")
    clean_model_id = configured_model_id.strip()
    if not clean_model_id:
        return DEFAULT_MODEL_ID
    # 配置值与 ACP 请求使用同一白名单，防止控制字符或超长值进入协议请求。
    if not MODEL_ID_PATTERN.fullmatch(clean_model_id):
        raise ValueError("配置项 model_id 格式无效。")
    return clean_model_id


class _PersistentSessionStore:
    """持久化全局 ACP 会话，并为跨 Codex 进程提供互斥锁。"""

    def __init__(self, path: Path = DEFAULT_SESSION_STATE_PATH) -> None:
        """使用固定状态文件保存会话标识；临时令牌永不写入磁盘。"""

        self.path = path
        self.lock_path = path.with_suffix(f"{path.suffix}.lock")

    def acquire_lock(self) -> BinaryIO:
        """获取跨进程独占锁，避免不同 Codex 的 /clear 与业务提示交叉执行。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.lock_path.open("a+b")
        try:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            return lock_file
        except Exception:
            lock_file.close()
            raise

    @staticmethod
    def release_lock(lock_file: BinaryIO) -> None:
        """释放跨进程锁；文件本身保留以供后续进程继续使用。"""

        try:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

    def read(self) -> str | None:
        """读取并校验持久会话标识；文件不存在表示尚未创建全局会话。"""

        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AcpError("无法读取持久会话状态文件。") from exc
        session_id = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 256:
            raise AcpError("持久会话状态文件包含无效 session_id。")
        return session_id.strip()

    def write(self, session_id: str) -> None:
        """以 UTF-8 原子写入会话标识，避免并发或进程中断留下半个 JSON。"""

        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 256:
            raise ValueError("session_id 必须是长度不超过 256 的非空字符串。")
        clean_session_id = session_id.strip()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f"{self.path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump({"session_id": clean_session_id}, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            with contextlib.suppress(OSError):
                os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def clear(self, expected_session_id: str) -> None:
        """仅在磁盘仍保存预期旧值时删除状态，防止误删其他进程的新会话。"""

        if self.read() == expected_session_id:
            self.path.unlink(missing_ok=True)


class _AcpClientPool:
    """缓存 ACP 连接，并让多个 Codex 进程串行复用同一持久会话。"""

    def __init__(self, session_store: _PersistentSessionStore | None = None) -> None:
        """初始化连接池；ACP 连接懒创建，会话标识从全局状态文件恢复。"""

        self._client: WorkBuddyAcpClient | None = None
        self._lock = asyncio.Lock()
        self._session_store = session_store or _PersistentSessionStore()
        self._session_id: str | None = None
        self._cwd: Path | None = None
        self._binding: str | None = None

    async def run(
        self,
        *,
        prompt: str,
        cwd: Path | None,
        session_id: str | None,
        timeout_seconds: float,
        permission_mode: PermissionMode,
        model_id: str,
        thought_level: ThoughtLevel,
    ) -> AcpRunResult:
        """选择、创建或复用任务会话，并串行执行一轮提示词。"""

        async with self._lock:
            process_lock = await asyncio.to_thread(self._session_store.acquire_lock)
            try:
                return await self._run_locked(
                    prompt=prompt,
                    cwd=cwd,
                    session_id=session_id,
                    timeout_seconds=timeout_seconds,
                    permission_mode=permission_mode,
                    model_id=model_id,
                    thought_level=thought_level,
                )
            finally:
                await asyncio.to_thread(self._session_store.release_lock, process_lock)

    async def _run_locked(
        self,
        *,
        prompt: str,
        cwd: Path | None,
        session_id: str | None,
        timeout_seconds: float,
        permission_mode: PermissionMode,
        model_id: str,
        thought_level: ThoughtLevel,
    ) -> AcpRunResult:
        """在进程锁保护下解析全局会话并执行一轮不可交叉的任务。"""

        gateway, active_session_id, active_cwd, binding = self._resolve_binding(
            requested_session_id=session_id,
            requested_cwd=cwd,
        )
        client = self._client
        if client is not None and client.gateway.base_url != gateway.base_url:
            await client.close()
            client = None
            self._client = None
        if client is None:
            client = WorkBuddyAcpClient(gateway, permission_mode=permission_mode)
            self._client = client
        else:
            # 连接按会话复用，但每轮权限仍严格服从本次 MCP 调用参数。
            client.permission_mode = permission_mode
        try:
            try:
                result = await client.run(
                    prompt,
                    cwd=active_cwd,
                    session_id=active_session_id,
                    timeout_seconds=timeout_seconds,
                    model_id=model_id,
                    thought_level=thought_level,
                )
            except AcpSessionUnavailableError:
                if active_session_id is None:
                    raise
                # session/load 尚未执行用户提示，只有这个阶段允许安全创建新会话并重试。
                self._session_store.clear(active_session_id)
                result = await client.run(
                    prompt,
                    cwd=active_cwd,
                    session_id=None,
                    timeout_seconds=timeout_seconds,
                    model_id=model_id,
                    thought_level=thought_level,
                )
                active_session_id = None
                binding = "recreated_session"
            self._session_store.write(result.session_id)
            self._session_id = result.session_id
            self._cwd = active_cwd
            self._binding = binding
            result.binding = binding
            result.session_reused = active_session_id is not None
            return result
        except AcpError:
            # 不自动重放失败任务，避免发送消息等外部写操作被重复执行。
            self._client = None
            await client.close()
            raise

    def snapshot(self) -> dict[str, Any]:
        """返回不含令牌的当前内存绑定状态。"""

        return {
            "session_id": self._session_id,
            "cwd": str(self._cwd) if self._cwd else None,
            "binding": self._binding,
            "connected": self._client is not None,
        }

    def _resolve_binding(
        self,
        *,
        requested_session_id: str | None,
        requested_cwd: Path | None,
    ) -> tuple[GatewayEndpoint, str | None, Path, str]:
        """按显式会话、进程缓存、新会话的优先级解析唯一绑定。"""

        discovery = GatewayDiscovery()
        gateway = self._client.gateway if self._client is not None else discovery.discover_host()
        if requested_session_id:
            if self._client is not None and requested_session_id == self._session_id:
                return (
                    self._client.gateway,
                    requested_session_id,
                    self._cwd or requested_cwd or Path.cwd(),
                    self._binding or "cached_task",
                )
            return (
                gateway,
                requested_session_id,
                requested_cwd or gateway.cwd or Path.cwd(),
                "explicit_session",
            )
        persisted_session_id = self._session_store.read()
        if persisted_session_id is not None:
            if self._client is not None and persisted_session_id == self._session_id:
                return (
                    self._client.gateway,
                    persisted_session_id,
                    self._cwd or requested_cwd or Path.cwd(),
                    self._binding or "persistent_session",
                )
            return (
                gateway,
                persisted_session_id,
                requested_cwd or gateway.cwd or Path.cwd(),
                "persistent_session",
            )
        if self._client is not None and self._session_id is not None:
            return (
                self._client.gateway,
                self._session_id,
                self._cwd or requested_cwd or Path.cwd(),
                self._binding or "cached_task",
            )
        # 仅在磁盘和内存都没有会话时创建，成功后立即写入全局状态文件。
        return gateway, None, requested_cwd or gateway.cwd or Path.cwd(), "created_session"


_client_pool = _AcpClientPool()


@server.list_tools()
async def list_tools() -> list[Tool]:
    """列出提供给 Codex 的 WorkBuddy 桥接工具。"""

    default_model_id = _load_default_model_id()
    common_properties = {
        "prompt": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100000,
            "description": "交给 WorkBuddy 执行的自然语言任务。",
        },
        "cwd": {
            "type": "string",
            "description": "创建或加载全局 WorkBuddy 会话时使用；不填则沿用隐藏 Host 目录。",
        },
        "timeout_seconds": {
            "type": "number",
            "minimum": 1,
            "maximum": 1800,
            "default": 300,
            "description": "任务超时秒数。",
        },
        "permission_mode": {
            "type": "string",
            "enum": ["deny", "allow_once"],
            "default": "deny",
            "description": (
                "deny 拒绝全部权限请求；allow_once 仅选择 WorkBuddy 提供的单次授权，"
                "永不授予永久权限。发送消息、修改文档等外部写操作必须显式使用 allow_once。"
            ),
        },
        "model_id": {
            "type": "string",
            "default": default_model_id,
            "description": (
                "WorkBuddy 模型 ID；默认读取项目根目录 config.json 的 model_id，"
                f"未配置时使用 {DEFAULT_MODEL_ID}，由 WorkBuddy 自动选模。"
            ),
        },
        "thought_level": {
            "type": "string",
            "enum": ["disabled", "minimal", "low", "medium", "high", "xhigh", "max", "enabled"],
            "default": DEFAULT_THOUGHT_LEVEL,
            "description": "WorkBuddy 思考强度；默认 disabled，关闭扩展思考以降低调用耗时。",
        },
    }
    return [
        Tool(
            name="workbuddy_status",
            description="检查 WorkBuddy 是否正在运行，并发现本机 ACP Gateway。",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        Tool(
            name="workbuddy_run",
            description=(
                "创建或恢复所有 Codex 进程共用的持久 WorkBuddy ACP 会话，每轮任务前自动"
                "执行 /clear。通过其已登录连接器读取腾讯文档、发送飞书消息等。"
                "默认只读；任何外部写操作都必须显式设置 permission_mode=allow_once。"
            ),
            inputSchema={
                "type": "object",
                "properties": common_properties,
                "required": ["prompt"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="workbuddy_continue",
            description="加载指定 WorkBuddy ACP 会话，执行 /clear 后继续一轮任务。",
            inputSchema={
                "type": "object",
                "properties": {
                    **common_properties,
                    "session_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 256,
                        "description": "workbuddy_run 返回的 ACP 会话 ID。",
                    },
                },
                "required": ["session_id", "prompt"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="workbuddy_cancel",
            description="取消指定 WorkBuddy ACP 会话中正在执行的任务。",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 256,
                    }
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """校验并分发 MCP 工具调用，错误以结构化文本返回。"""

    try:
        if name == "workbuddy_status":
            result = _status()
        elif name == "workbuddy_run":
            result = await _run_task(arguments, session_id=None)
        elif name == "workbuddy_continue":
            session_id = arguments.get("session_id")
            result = await _run_task(arguments, session_id=session_id)
        elif name == "workbuddy_cancel":
            result = await _cancel_task(arguments)
        else:
            raise ValueError(f"未知工具：{name}")
        return [_json_content({"ok": True, "result": result})]
    except (AcpError, GatewayDiscoveryError, OSError, ValueError) as exc:
        # 仅返回经过控制的异常消息，不把临时令牌、请求头或堆栈暴露给 MCP 客户端。
        return [_json_content({"ok": False, "error": str(exc)})]


def _status() -> dict[str, Any]:
    """发现 Gateway 并返回不含凭据的运行状态。"""

    discovery = GatewayDiscovery()
    gateway = discovery.discover_host()
    return {
        "available": True,
        "gateway": gateway.base_url,
        "pid": gateway.pid,
        "last_heartbeat": gateway.last_heartbeat,
        "cached_task": _client_pool.snapshot(),
    }


async def _run_task(arguments: dict[str, Any], session_id: Any) -> dict[str, Any]:
    """提取 MCP 参数并执行新会话或续接会话。"""

    prompt = arguments.get("prompt")
    cwd_value = arguments.get("cwd")
    timeout_seconds = arguments.get("timeout_seconds", 300)
    permission_mode = _permission_mode(arguments.get("permission_mode", "deny"))
    # 调用参数拥有最高优先级；省略参数时才读取配置，保留单次任务切换模型的能力。
    model_id = arguments["model_id"] if "model_id" in arguments else _load_default_model_id()
    thought_level = _thought_level(arguments.get("thought_level", DEFAULT_THOUGHT_LEVEL))
    if not isinstance(prompt, str):
        raise ValueError("prompt 必须是字符串。")
    if cwd_value is not None and not isinstance(cwd_value, str):
        raise ValueError("cwd 必须是字符串。")
    if session_id is not None and not isinstance(session_id, str):
        raise ValueError("session_id 必须是字符串。")
    if not isinstance(model_id, str):
        raise ValueError("model_id 必须是字符串。")

    cwd = Path(cwd_value) if cwd_value else None
    protected_prompt = _apply_permission_boundary(prompt, permission_mode)
    result = await _client_pool.run(
        prompt=protected_prompt,
        cwd=cwd,
        session_id=session_id,
        timeout_seconds=timeout_seconds,
        permission_mode=permission_mode,
        model_id=model_id,
        thought_level=thought_level,
    )
    return result.to_dict()


async def _cancel_task(arguments: dict[str, Any]) -> dict[str, Any]:
    """连接 Gateway 并发送指定会话的取消通知。"""

    session_id = arguments.get("session_id")
    if not isinstance(session_id, str):
        raise ValueError("session_id 必须是字符串。")
    gateway = GatewayDiscovery().discover()
    async with WorkBuddyAcpClient(gateway, permission_mode="deny") as client:
        await client.connect()
        await client.cancel(session_id)
    return {"session_id": session_id, "cancelled": True}


def _apply_permission_boundary(prompt: str, permission_mode: PermissionMode) -> str:
    """在任务前加入与协议权限模式一致的明确安全边界。"""

    if permission_mode == "deny":
        boundary = (
            "[桥接器安全边界] 本次任务只允许读取、检索和总结。不得发送消息、修改文档、"
            "创建或删除外部数据；如无法只读完成，请明确说明。\n\n"
        )
    else:
        boundary = (
            "[桥接器安全边界] 用户仅对本次任务显式授予单次外部操作权限。"
            "不得申请永久授权，不得执行任务描述之外的写操作。\n\n"
        )
    return f"{boundary}{prompt}"


def _permission_mode(value: Any) -> PermissionMode:
    """校验 MCP 输入中的权限模式。"""

    if not isinstance(value, str) or value not in {"deny", "allow_once"}:
        raise ValueError("permission_mode 只能是 deny 或 allow_once。")
    return value


def _thought_level(value: Any) -> ThoughtLevel:
    """校验 MCP 输入中的 WorkBuddy 思考强度。"""

    allowed = {"disabled", "minimal", "low", "medium", "high", "xhigh", "max", "enabled"}
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("thought_level 值无效。")
    return value


def _json_content(payload: dict[str, Any]) -> TextContent:
    """把结构化结果编码为 UTF-8 友好的 MCP 文本内容。"""

    return TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))


async def run_server() -> None:
    """启动 MCP stdio 传输并持续服务到客户端断开。"""

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=SERVER_NAME,
                server_version=SERVER_VERSION,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
                instructions=(
                    "通过 WorkBuddy 使用其连接器。外部写操作必须显式传入 "
                    "permission_mode=allow_once。"
                ),
            ),
        )


def main() -> None:
    """命令行入口。"""

    asyncio.run(run_server())


if __name__ == "__main__":
    main()
