# WorkBuddy ACP Bridge

把 WorkBuddy 已登录的连接器能力通过 MCP 暴露给支持 MCP 的 CLI 客户端（本文以 Codex CLI 为例，其他兼容 CLI 也可以使用）。桥接器不直接保存飞书、腾讯文档等连接器凭据，而是实时发现 WorkBuddy 本机 Remote Gateway，再通过 ACP 让 WorkBuddy 自己完成任务。

## 工作方式

```text
支持 MCP 的 CLI 客户端
（以 Codex CLI 为例）
          │ MCP stdio
          ▼
workbuddy-acp-bridge
   │ ACP HTTP + SSE（仅 127.0.0.1 / localhost）
   ▼
WorkBuddy Remote Gateway
   │
   ├─ 飞书连接器
   ├─ 腾讯文档连接器
   └─ WorkBuddy 中已启用的其他连接器
```

本方案绕开了 WorkBuddy 自动化的小时级轮询限制：CLI 调用会实时进入 ACP 通道，
不同 CLI/MCP 进程会串行复用同一个持久化 WorkBuddy 会话。

## 当前能力

- `workbuddy_status`：检查 WorkBuddy Gateway 是否可用。
- `workbuddy_run`：创建或恢复全局隐藏 ACP 会话，所有 CLI 进程复用同一 `session_id`。
- `workbuddy_continue`：使用返回的 `session_id` 继续会话。
- `workbuddy_cancel`：取消会话中正在执行的任务。
- 自动适配 WorkBuddy 每次启动后变化的 Gateway 端口。
- 将 `session_id` 持久化到 `%USERPROFILE%\.workbuddy\codex-acp-bridge-session.json`。
- 每轮业务提示前先执行 `/clear`，避免不同 CLI 客户端的上下文互相干扰。
- 使用跨进程文件锁串行执行 `/clear` 和业务提示，防止两轮任务交叉。
- 支持通过项目根目录 `config.json` 配置默认模型，未配置时使用 `auto` 自动选模。
- 默认拒绝权限申请；仅在调用方显式传入 `permission_mode=allow_once` 时选择单次授权。
- 不选择 `allow_always`，不落盘 Gateway 临时令牌，也不在结果中输出令牌。

## 环境要求

- Windows 11。
- Python 3.11 或更高版本。
- Codex CLI，或其他支持 MCP stdio 的 CLI 客户端。
- WorkBuddy 已启动。
- 需要使用的连接器已在 WorkBuddy 中启用并完成登录。

## 安装

在 PowerShell 中执行：

```powershell
cd "E:\个人项目\workbuddy-acp-bridge"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

以 Codex CLI 为例注册 MCP 服务：

```powershell
codex mcp add workbuddy -- "E:\个人项目\workbuddy-acp-bridge\.venv\Scripts\python.exe" -m workbuddy_acp_bridge.server
```

使用其他支持 MCP stdio 的 CLI 时，按该客户端的配置方式注册同一 Python 启动命令即可。

检查配置：

```powershell
codex mcp list
```

重新启动对应的 CLI 客户端，使新 MCP 服务进入新会话。

## 使用示例

### 检查连接

在 CLI 客户端中输入（以下以 Codex 为例）：

```text
调用 workbuddy_status，检查 WorkBuddy 是否已连接。
```

### 读取腾讯文档

读取任务保持默认的 `permission_mode=deny`：

```text
通过 WorkBuddy 查找腾讯文档《项目周报》，总结本周风险。只读，不要修改文档。
```

### 发送飞书消息

发送消息属于外部写操作，需要在任务中明确要求 CLI 客户端使用 `permission_mode=allow_once`：

```text
调用 workbuddy_run，permission_mode 使用 allow_once。
让 WorkBuddy 给飞书联系人张三发送：“会议改到下午 3 点”，只发送一次。
```

建议让 CLI 客户端在调用前复述收件人和消息正文。桥接器只负责协议层的单次授权，业务内容仍应由使用者确认。

### 全局持久会话

所有 CLI/MCP 进程通常只需要调用 `workbuddy_run`：

1. 第一次调用通过 WorkBuddy Host Runtime 创建隐藏 ACP 会话并持久化 `session_id`。
2. 其他 CLI 进程从状态文件恢复同一个会话，不依赖原 MCP 进程继续存活。
3. 每轮任务先执行 `/clear`，成功后才发送业务提示；清理失败时业务提示不会执行。
4. 多个 CLI 客户端同时调用时通过文件锁排队，确保 `/clear` 与其业务提示不可被另一轮插入。
5. 仅当 `session/load` 被 WorkBuddy 明确拒绝时，才删除旧 ID、创建新会话并更新状态文件。
6. 超时、传输失败或业务提示失败不会自动重建、重放，避免外部写操作重复执行。
7. 不扫描、不绑定 WorkBuddy 界面里当前打开的任务，也不创建时间戳界面任务。

返回结果中的 `binding` 用于说明来源：`created_session` 表示首次创建，
`persistent_session` 表示从全局状态恢复，`recreated_session` 表示旧会话失效后重建，
`explicit_session` 表示显式续接。状态文件只包含 `session_id`，不保存 Gateway 临时令牌。

### 显式继续已有任务

如需切换到指定历史会话，可以使用 `workbuddy_continue`：

```text
调用 workbuddy_continue，使用刚才返回的 session_id，继续询问文档中的负责人是谁。
```

## 权限模型

| 模式 | 行为 | 适用场景 |
|---|---|---|
| `deny` | 拒绝 WorkBuddy 发来的全部权限请求 | 查询、读取、搜索、总结 |
| `allow_once` | 只选择 `kind=allow_once` 的选项；没有单次选项就拒绝 | 发送消息、修改文档等明确的一次性操作 |

桥接器还会在提示词前加入对应的安全边界。该提示词约束是辅助措施，真正的授权边界由 ACP 权限响应实现。

## 配置

### 默认模型

默认模型从项目根目录的 `config.json` 读取：

```json
{
  "model_id": "custom-local:kimi-k2.6"
}
```

需要切换模型时，修改 `model_id` 为 WorkBuddy 当前可用的模型 ID。配置优先级从高到低为：

1. 调用 `workbuddy_run` 或 `workbuddy_continue` 时显式传入的 `model_id`。
2. 项目根目录 `config.json` 中的 `model_id`。
3. 内置默认值 `auto`，由 WorkBuddy 自动选择模型。

配置文件不存在，或者 `model_id` 字段缺失、为 `null`、空字符串时，会自动使用 `auto`。也可以显式传入或配置 `auto`，让 WorkBuddy 自动选择模型。

`model_id` 只能包含英文字母、数字、点、下划线、冒号和连字符，长度为 1 至 128 个字符。配置文件必须是 UTF-8 JSON、根节点必须是对象，文件大小不能超过 64 KiB；格式或字段类型错误时会返回明确错误，避免错误配置被静默忽略。

桥接器会在列出工具和每次执行任务时重新读取配置，因此修改后下一次任务即可生效。CLI 客户端已缓存工具定义时，界面显示的默认值可能需要重新连接 MCP 后才会刷新，但实际执行仍以最新配置为准。

### WorkBuddy 数据目录

默认从 `%USERPROFILE%\.workbuddy\sessions\*.json` 发现 Gateway。若 WorkBuddy 数据目录不同，可在 CLI 客户端注册 MCP 服务时设置环境变量。以 Codex CLI 为例：

```powershell
codex mcp add workbuddy --env WORKBUDDY_HOME="D:\CustomWorkBuddyData" -- "E:\个人项目\workbuddy-acp-bridge\.venv\Scripts\python.exe" -m workbuddy_acp_bridge.server
```

安全限制如下：

- Gateway 只允许 `http://127.0.0.1:<port>`、`http://localhost:<port>` 或 IPv6 回环地址。
- 会话文件最大读取 64 KiB，并阻止符号链接逃逸 `sessions` 目录。
- 提示词最大 100,000 字符，返回代理文本最大 1,000,000 字符。
- 单次任务超时范围为 1 至 1,800 秒。

## 测试

```powershell
cd "E:\个人项目\workbuddy-acp-bridge"
python -m pytest -q
```

测试使用本地模拟数据，不会发送飞书消息或修改腾讯文档。

## 故障排查

### 未找到 WorkBuddy 会话目录

先启动 WorkBuddy，并确认 `%USERPROFILE%\.workbuddy\sessions` 下存在 JSON 会话文件。

### 找不到可用 Gateway

WorkBuddy 重启时端口会变化。保持 WorkBuddy 运行，再重新调用 `workbuddy_status`；桥接器每次都会重新发现端口，不需要手工更新。

### 读取任务被拒绝

部分连接器可能连读取也要求权限。不要直接改成永久授权；确认任务和目标后，再显式使用 `allow_once`。

### WorkBuddy 升级后协议失败

此项目使用 WorkBuddy 当前内置的 ACP Gateway，而它不是面向第三方保证稳定的公开接口。若升级后出现建连、SSE 或字段错误，需要重新核对 `app.asar` 中的 ACP 传输实现并更新桥接器。

## 声明

我非常喜欢 WorkBuddy，也十分尊重和欣赏 WorkBuddy 背后的团队。本项目仅用于个人学习、研究和效率工具探索，与 WorkBuddy 官方及其团队不存在隶属、授权或合作关系。

如权利方认为本项目存在侵权或不当使用，请通过项目仓库联系我。我会第一时间核实处理，并在确认需要时及时删除或下架相关内容。
