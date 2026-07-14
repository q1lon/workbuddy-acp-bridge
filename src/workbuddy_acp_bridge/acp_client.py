"""WorkBuddy ACP HTTP/SSE 客户端。

@author 李杰
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from .gateway import GatewayDiscovery, GatewayEndpoint


PermissionMode = Literal["deny", "allow_once"]
ThoughtLevel = Literal["disabled", "minimal", "low", "medium", "high", "xhigh", "max", "enabled"]
DEFAULT_MODEL_ID = "auto"
DEFAULT_THOUGHT_LEVEL: ThoughtLevel = "disabled"
MAX_PROMPT_CHARS = 100_000
MAX_OUTPUT_CHARS = 1_000_000
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class AcpError(RuntimeError):
    """表示 ACP 连接、协议或请求失败。"""


class AcpRequestError(AcpError):
    """表示 ACP 服务端通过 JSON-RPC 明确拒绝了某个请求。"""

    def __init__(self, message: str, *, code: int | str | None = None) -> None:
        """保存服务端错误消息和可选错误码，供调用阶段安全分类。"""

        super().__init__(message)
        self.code = code


class AcpSessionUnavailableError(AcpError):
    """表示 session/load 已明确失败，可在用户提示执行前安全重建会话。"""


@dataclass(slots=True)
class AcpRunResult:
    """保存一次 WorkBuddy 任务的可序列化结果。"""

    session_id: str
    text: str
    model_id: str
    thought_level: str
    stop_reason: str | None = None
    permissions: list[dict[str, str]] = field(default_factory=list)
    session_reused: bool = False
    binding: str | None = None
    cwd: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """返回适合 MCP 文本结果的字典。"""

        return {
            "session_id": self.session_id,
            "text": self.text,
            "model_id": self.model_id,
            "thought_level": self.thought_level,
            "stop_reason": self.stop_reason,
            "permissions": self.permissions,
            "session_reused": self.session_reused,
            "binding": self.binding,
            "cwd": self.cwd,
        }


class WorkBuddyAcpClient:
    """通过 WorkBuddy Remote Gateway 执行 ACP 会话。"""

    def __init__(
        self,
        gateway: GatewayEndpoint,
        *,
        permission_mode: PermissionMode = "deny",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """创建客户端；连接令牌仅保存在当前对象内存中。"""

        self.gateway = gateway
        self.permission_mode = self._validate_permission_mode(permission_mode)
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=None),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            # Gateway 只监听本机回环地址，继承企业 SOCKS/HTTP 代理既无意义又会显著拖慢请求。
            trust_env=False,
        )
        self._connection_id: str | None = None
        self._session_token: str | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._listener_task: asyncio.Task[None] | None = None
        self._post_tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._text_chunks: list[str] = []
        self._output_chars = 0
        self._permissions: list[dict[str, str]] = []
        self._loaded_sessions: set[str] = set()
        self._session_options: dict[str, tuple[str, str]] = {}
        self._collect_output_session_id: str | None = None

    async def connect(self) -> None:
        """建立 ACP 连接并完成协议初始化。"""

        if self._connection_id:
            return
        try:
            response = await self._http.post(
                self.gateway.connect_url,
                headers={"X-CodeBuddy-Request": "1", "Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AcpError("无法连接 WorkBuddy ACP Gateway。") from exc

        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        connection_id = payload.get("connectionId") if isinstance(payload, dict) else None
        session_token = payload.get("sessionToken") if isinstance(payload, dict) else None
        if not isinstance(connection_id, str) or not connection_id:
            raise AcpError("WorkBuddy ACP 建连响应缺少 connectionId。")
        if session_token is not None and not isinstance(session_token, str):
            raise AcpError("WorkBuddy ACP 建连响应包含无效 sessionToken。")

        self._connection_id = connection_id
        self._session_token = session_token
        self._listener_task = asyncio.create_task(self._listen_sse())
        await asyncio.sleep(0)
        await self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "workbuddy-acp-bridge", "version": "0.1.0"},
                "clientCapabilities": {
                    "_meta": {"codebuddy.ai": {"question": False, "promptSuggestion": False}}
                },
            },
            timeout_seconds=30.0,
        )

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        session_id: str | None = None,
        timeout_seconds: float = 300.0,
        model_id: str = DEFAULT_MODEL_ID,
        thought_level: ThoughtLevel = DEFAULT_THOUGHT_LEVEL,
    ) -> AcpRunResult:
        """创建或加载会话，并执行一轮提示词。"""

        clean_prompt = self._validate_prompt(prompt)
        clean_cwd = self._validate_cwd(cwd)
        clean_timeout = self._validate_timeout(timeout_seconds)
        clean_model_id = self._validate_model_id(model_id)
        clean_thought_level = self._validate_thought_level(thought_level)
        # 客户端允许跨 MCP 调用复用，因此每轮开始必须隔离上一次的输出与权限记录。
        self._text_chunks = []
        self._output_chars = 0
        self._permissions = []
        await self.connect()

        session_reused = session_id is not None
        if session_id is None:
            session_response = await self.request(
                "session/new",
                {"cwd": clean_cwd, "mcpServers": []},
                timeout_seconds=30.0,
            )
            active_session_id = self._extract_session_id(session_response)
            self._loaded_sessions.add(active_session_id)
        else:
            active_session_id = self._validate_session_id(session_id)
            try:
                session_response = await self.request(
                    "session/load",
                    {"sessionId": active_session_id, "cwd": clean_cwd, "mcpServers": []},
                    timeout_seconds=30.0,
                )
            except AcpRequestError as exc:
                # 远端明确拒绝 load 时，业务提示尚未执行，调用方可安全创建替代会话。
                raise AcpSessionUnavailableError("持久 ACP 会话已失效或无法加载。") from exc
            self._loaded_sessions.add(active_session_id)

        previous_options = self._session_options.get(active_session_id)
        if clean_model_id != "auto" and (
            previous_options is None or previous_options[0] != clean_model_id
        ):
            self._ensure_model_available(clean_model_id, session_response)
            await self.request(
                "session/set_model",
                {"sessionId": active_session_id, "modelId": clean_model_id},
                timeout_seconds=30.0,
            )
        if previous_options is None or previous_options[1] != clean_thought_level:
            await self.request(
                "session/set_config_option",
                {
                    "sessionId": active_session_id,
                    "configId": "thought_level",
                    "value": clean_thought_level,
                },
                timeout_seconds=30.0,
            )
        self._session_options[active_session_id] = (clean_model_id, clean_thought_level)

        # 多个 Codex 共享会话时必须先清空历史，清理失败则禁止继续执行用户任务。
        await self.request(
            "session/prompt",
            {
                "sessionId": active_session_id,
                "prompt": [{"type": "text", "text": "/clear"}],
            },
            timeout_seconds=min(clean_timeout, 30.0),
        )
        self._text_chunks = []
        self._output_chars = 0
        self._permissions = []
        self._collect_output_session_id = active_session_id
        try:
            result = await self.request(
                "session/prompt",
                {
                    "sessionId": active_session_id,
                    "prompt": [{"type": "text", "text": clean_prompt}],
                },
                timeout_seconds=clean_timeout,
            )
        finally:
            self._collect_output_session_id = None
        stop_reason = result.get("stopReason") if isinstance(result, dict) else None
        return AcpRunResult(
            session_id=active_session_id,
            text="".join(self._text_chunks),
            model_id=clean_model_id,
            thought_level=clean_thought_level,
            stop_reason=stop_reason if isinstance(stop_reason, str) else None,
            permissions=list(self._permissions),
            session_reused=session_reused,
            cwd=clean_cwd,
        )

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> Any:
        """发送 JSON-RPC 请求并等待 POST 或 GET SSE 返回结果。"""

        if not self._connection_id:
            raise AcpError("ACP 尚未连接。")
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        post_task = asyncio.create_task(self._drive_post(message, request_id))
        self._post_tasks.add(post_task)
        post_task.add_done_callback(self._post_tasks.discard)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout_seconds)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            if method == "session/prompt":
                session_id = params.get("sessionId")
                if isinstance(session_id, str):
                    await self.cancel(session_id)
            raise AcpError(f"WorkBuddy ACP 请求超时：{method}。") from exc

    async def cancel(self, session_id: str) -> None:
        """通知 WorkBuddy 取消指定 ACP 会话当前任务。"""

        if not self._connection_id:
            return
        clean_session_id = self._validate_session_id(session_id)
        await self._post_message(
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": clean_session_id}}
        )

    async def close(self) -> None:
        """关闭 ACP 连接并从内存清除临时令牌。"""

        if self._closed:
            return
        self._closed = True
        if self._connection_id:
            with contextlib.suppress(httpx.HTTPError):
                await self._http.delete(self.gateway.acp_url, headers=self._headers())
        if self._listener_task:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
        for task in tuple(self._post_tasks):
            task.cancel()
        self._connection_id = None
        self._session_token = None
        if self._owns_http_client:
            await self._http.aclose()

    async def __aenter__(self) -> WorkBuddyAcpClient:
        """进入异步上下文并返回当前客户端。"""

        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        """退出异步上下文时可靠关闭连接。"""

        await self.close()

    async def _drive_post(self, message: dict[str, Any], request_id: int) -> None:
        """执行请求传输，并把传输错误投递给对应 Future。"""

        try:
            await self._post_message(message)
        except Exception:
            pending = self._pending.pop(request_id, None)
            if pending and not pending.done():
                pending.set_exception(AcpError("WorkBuddy ACP 传输失败。"))

    async def _post_message(self, message: dict[str, Any]) -> None:
        """以流式方式 POST JSON-RPC，避免权限请求形成互相等待。"""

        try:
            async with self._http.stream(
                "POST",
                self.gateway.acp_url,
                headers=self._headers(),
                json=message,
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" in content_type:
                    await self._consume_sse(response)
                else:
                    body = await response.aread()
                    if body.strip():
                        await self._handle_message(json.loads(body))
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise AcpError("WorkBuddy ACP HTTP 请求失败。") from exc

    async def _listen_sse(self) -> None:
        """维持 GET SSE 通道，接收异步响应和会话通知。"""

        while not self._closed and self._connection_id:
            try:
                async with self._http.stream(
                    "GET",
                    self.gateway.acp_url,
                    headers={
                        "Accept": "text/event-stream",
                        "acp-connection-id": self._connection_id,
                    },
                ) as response:
                    response.raise_for_status()
                    await self._consume_sse(response)
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, AcpError):
                # Gateway 重启或通道短暂断开时允许重连，具体请求仍由自身超时控制。
                await asyncio.sleep(0.25)

    async def _consume_sse(self, response: httpx.Response) -> None:
        """增量解析 SSE message 事件并分发 JSON-RPC 消息。"""

        event_name = "message"
        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if line == "":
                await self._dispatch_sse_event(event_name, data_lines)
                event_name = "message"
                data_lines = []
            elif line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        await self._dispatch_sse_event(event_name, data_lines)

    async def _dispatch_sse_event(self, event_name: str, data_lines: list[str]) -> None:
        """解析单个 SSE 事件；非 message 事件被安全忽略。"""

        if event_name != "message" or not data_lines:
            return
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError as exc:
            raise AcpError("WorkBuddy 返回了无效 SSE JSON。") from exc
        await self._handle_message(payload)

    async def _handle_message(self, message: Any) -> None:
        """处理 JSON-RPC 响应、通知及服务器侧请求。"""

        if not isinstance(message, dict):
            return
        request_id = message.get("id")
        method = message.get("method")
        if request_id is not None and not isinstance(method, str):
            self._resolve_response(request_id, message)
            return
        if isinstance(method, str) and request_id is not None:
            if method in {"session/request_permission", "requestPermission"}:
                result = self._permission_result(message.get("params"))
            elif method == "elicitation/create":
                result = {"action": "cancel"}
            else:
                # 问答和未知扩展请求不能由桥接器猜测用户意图，统一取消。
                result = {"outcome": {"outcome": "cancelled"}}
            await self._send_result(request_id, result)
            return
        if method in {"session/update", "sessionUpdate"}:
            self._record_session_update(message.get("params"))

    def _resolve_response(self, request_id: Any, message: dict[str, Any]) -> None:
        """完成对应 JSON-RPC 请求的 Future。"""

        if not isinstance(request_id, int) or isinstance(request_id, bool):
            return
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            error_message = error.get("message")
            error_code = error.get("code")
            future.set_exception(
                AcpRequestError(
                    error_message if isinstance(error_message, str) else "ACP 请求失败。",
                    code=error_code if isinstance(error_code, (int, str)) else None,
                )
            )
        else:
            future.set_result(message.get("result"))

    async def _send_result(self, request_id: Any, result: dict[str, Any]) -> None:
        """向 WorkBuddy 回传服务器侧请求的 JSON-RPC 结果。"""

        if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
            return
        await self._post_message({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _permission_result(self, params: Any) -> dict[str, Any]:
        """按显式授权模式选择单次授权，其他情况一律取消。"""

        tool_name = "unknown"
        options: list[Any] = []
        if isinstance(params, dict):
            raw_tool = params.get("toolCall")
            if isinstance(raw_tool, dict):
                raw_input = raw_tool.get("rawInput")
                if isinstance(raw_input, dict) and isinstance(raw_input.get("name"), str):
                    tool_name = raw_input["name"][:200]
            raw_options = params.get("options")
            if isinstance(raw_options, list):
                options = raw_options

        if self.permission_mode == "allow_once":
            for option in options:
                # 永不选择 allow_always，避免一次 MCP 调用形成持久外部写权限。
                if isinstance(option, dict) and option.get("kind") == "allow_once":
                    option_id = option.get("optionId")
                    if isinstance(option_id, str) and option_id:
                        self._permissions.append({"tool": tool_name, "decision": "allow_once"})
                        return {"outcome": {"outcome": "selected", "optionId": option_id}}

        self._permissions.append({"tool": tool_name, "decision": "cancelled"})
        return {"outcome": {"outcome": "cancelled"}}

    def _record_session_update(self, params: Any) -> None:
        """从会话通知中提取代理文本，并限制累计输出大小。"""

        if not isinstance(params, dict):
            return
        if self._collect_output_session_id is None:
            return
        update_session_id = params.get("sessionId")
        if isinstance(update_session_id, str) and update_session_id != self._collect_output_session_id:
            return
        update = params.get("update")
        if not isinstance(update, dict):
            return
        update_type = update.get("sessionUpdate")
        content = update.get("content")
        if update_type != "agent_message_chunk" or not isinstance(content, dict):
            return
        text = content.get("text")
        if not isinstance(text, str) or not text:
            return
        remaining = MAX_OUTPUT_CHARS - self._output_chars
        if remaining <= 0:
            return
        chunk = text[:remaining]
        self._text_chunks.append(chunk)
        self._output_chars += len(chunk)

    def _headers(self) -> dict[str, str]:
        """构造 ACP 请求头，但不对外暴露临时令牌。"""

        if not self._connection_id:
            raise AcpError("ACP 尚未连接。")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "X-CodeBuddy-Request": "1",
            "acp-connection-id": self._connection_id,
        }
        if self._session_token:
            headers["acp-session-token"] = self._session_token
        return headers

    @staticmethod
    def _validate_permission_mode(mode: str) -> PermissionMode:
        """校验权限模式，只接受拒绝或显式单次授权。"""

        if mode not in {"deny", "allow_once"}:
            raise ValueError("permission_mode 只能是 deny 或 allow_once。")
        return mode  # type: ignore[return-value]

    @staticmethod
    def _validate_prompt(prompt: str) -> str:
        """校验任务提示词的类型、空值和长度。"""

        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt 不能为空。")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"prompt 不能超过 {MAX_PROMPT_CHARS} 个字符。")
        return prompt.strip()

    @staticmethod
    def _validate_model_id(model_id: str) -> str:
        """校验模型标识，阻止控制字符或异常大输入进入 ACP 扩展方法。"""

        if not isinstance(model_id, str) or not MODEL_ID_PATTERN.fullmatch(model_id.strip()):
            raise ValueError("model_id 格式无效。")
        return model_id.strip()

    @staticmethod
    def _validate_thought_level(thought_level: str) -> ThoughtLevel:
        """校验 WorkBuddy 深度思考等级。"""

        allowed = {"disabled", "minimal", "low", "medium", "high", "xhigh", "max", "enabled"}
        if not isinstance(thought_level, str) or thought_level not in allowed:
            raise ValueError("thought_level 值无效。")
        return thought_level  # type: ignore[return-value]

    @staticmethod
    def _ensure_model_available(model_id: str, session_response: Any) -> None:
        """在 WorkBuddy 返回模型清单时确认目标模型仍然可用。"""

        if not isinstance(session_response, dict):
            return
        models = session_response.get("models")
        if not isinstance(models, dict):
            return
        available = models.get("availableModels")
        if not isinstance(available, list) or not available:
            return
        model_ids = {
            item.get("modelId") or item.get("id")
            for item in available
            if isinstance(item, dict)
        }
        if model_id not in model_ids:
            raise ValueError(f"WorkBuddy 当前不可用模型：{model_id}。")

    @staticmethod
    def _validate_cwd(cwd: Path) -> str:
        """校验会话工作目录存在且确实为目录。"""

        resolved = cwd.expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("cwd 必须是存在的目录。")
        return str(resolved)

    @staticmethod
    def _validate_timeout(timeout_seconds: float) -> float:
        """把超时时间限制在 1 秒至 30 分钟。"""

        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds 必须是数字。")
        if not 1 <= float(timeout_seconds) <= 1800:
            raise ValueError("timeout_seconds 必须在 1 到 1800 之间。")
        return float(timeout_seconds)

    @staticmethod
    def _validate_session_id(session_id: str) -> str:
        """校验 ACP 会话标识，防止异常大输入进入协议层。"""

        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id 不能为空。")
        if len(session_id) > 256:
            raise ValueError("session_id 过长。")
        return session_id.strip()

    @staticmethod
    def _extract_session_id(result: Any) -> str:
        """从 session/new 响应中提取并校验会话标识。"""

        session_id = result.get("sessionId") if isinstance(result, dict) else None
        return WorkBuddyAcpClient._validate_session_id(session_id)


def discover_client(
    *, permission_mode: PermissionMode = "deny", workbuddy_home: Path | None = None
) -> WorkBuddyAcpClient:
    """动态发现 Gateway 并创建尚未连接的 ACP 客户端。"""

    gateway = GatewayDiscovery(workbuddy_home).discover()
    return WorkBuddyAcpClient(gateway, permission_mode=permission_mode)
