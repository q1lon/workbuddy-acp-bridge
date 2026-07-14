"""测试 GatewayDiscovery 的跨平台会话文件发现逻辑。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from workbuddy_acp_bridge.gateway import (
    GatewayDiscovery,
    GatewayDiscoveryError,
    GatewayEndpoint,
)


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    """创建模拟的 WorkBuddy sessions 目录。"""
    sessions = tmp_path / ".workbuddy" / "sessions"
    sessions.mkdir(parents=True)
    return sessions


def _write_session(session_dir: Path, name: str, **overrides: object) -> Path:
    """写入一个模拟的 WorkBuddy 会话文件。"""
    data = {
        "endpoint": "http://127.0.0.1:51921",
        "pid": 12345,
        "lastHeartbeat": time.time() * 1000,
        "sessionId": f"session-{name}",
        "cwd": "/tmp/workbuddy-host-cli",
    }
    data.update(overrides)
    path = session_dir / f"{name}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_discover_returns_latest_heartbeat(session_dir: Path) -> None:
    _write_session(session_dir, "old", lastHeartbeat=1000.0)
    _write_session(session_dir, "new", lastHeartbeat=time.time() * 1000)
    discovery = GatewayDiscovery(session_dir.parent)
    gateway = discovery.discover()
    assert gateway.pid == 12345
    assert gateway.base_url == "http://127.0.0.1:51921"


def test_discover_ignores_stale_heartbeat(session_dir: Path) -> None:
    _write_session(session_dir, "stale", lastHeartbeat=1000.0)
    discovery = GatewayDiscovery(session_dir.parent)
    with pytest.raises(GatewayDiscoveryError, match="没有发现"):
        discovery.discover()


def test_discover_host_returns_host_only(session_dir: Path) -> None:
    _write_session(session_dir, "host", cwd="/tmp/workbuddy-host-cli")
    _write_session(session_dir, "non_host", cwd="/tmp/other")
    discovery = GatewayDiscovery(session_dir.parent)
    gateway = discovery.discover_host()
    # 应返回带 workbuddy-host-cli 标记的
    assert gateway.is_host
    assert gateway.cwd is not None
    assert "workbuddy-host-cli" in str(gateway.cwd).lower()


def test_discover_host_raises_when_no_host(session_dir: Path) -> None:
    _write_session(session_dir, "non_host", cwd="/tmp/other")
    discovery = GatewayDiscovery(session_dir.parent)
    with pytest.raises(GatewayDiscoveryError, match="没有发现 WorkBuddy Host Runtime"):
        discovery.discover_host()


def test_ignores_invalid_pid(session_dir: Path) -> None:
    _write_session(session_dir, "bad_pid", pid=0)
    discovery = GatewayDiscovery(session_dir.parent)
    with pytest.raises(GatewayDiscoveryError):
        discovery.discover()


def test_ignores_non_dict_json(session_dir: Path) -> None:
    (session_dir / "bad.json").write_text("[]", encoding="utf-8")
    discovery = GatewayDiscovery(session_dir.parent)
    with pytest.raises(GatewayDiscoveryError):
        discovery.discover()


def test_ignores_oversized_file(session_dir: Path) -> None:
    """超过 64 KiB 的会话文件应被忽略。"""
    big_data = {"endpoint": "http://127.0.0.1:51921", "pid": 12345, "lastHeartbeat": time.time() * 1000}
    big_text = json.dumps(big_data)
    path = session_dir / "big.json"
    path.write_text(big_text + "x" * (64 * 1024 + 1), encoding="utf-8")
    discovery = GatewayDiscovery(session_dir.parent)
    with pytest.raises(GatewayDiscoveryError):
        discovery.discover()


def test_ignores_symlink_escape(session_dir: Path) -> None:
    """符号链接指向 sessions 目录外时应被忽略。"""
    outside = session_dir.parent / "outside.json"
    outside.write_text(
        json.dumps({"endpoint": "http://127.0.0.1:51921", "pid": 12345, "lastHeartbeat": time.time() * 1000}),
        encoding="utf-8",
    )
    link = session_dir / "escaped.json"
    link.symlink_to(outside)
    discovery = GatewayDiscovery(session_dir.parent)
    with pytest.raises(GatewayDiscoveryError):
        discovery.discover()


def test_validate_base_url_loopback_only() -> None:
    GatewayDiscovery.validate_base_url("http://127.0.0.1:51921")
    GatewayDiscovery.validate_base_url("http://localhost:51921")
    GatewayDiscovery.validate_base_url("http://[::1]:51921")
    with pytest.raises(GatewayDiscoveryError, match="仅允许使用本机 HTTP 回环地址"):
        GatewayDiscovery.validate_base_url("http://example.com:51921")
    with pytest.raises(GatewayDiscoveryError, match="仅允许使用本机 HTTP 回环地址"):
        GatewayDiscovery.validate_base_url("https://127.0.0.1:51921")


def test_validate_base_url_rejects_credentials() -> None:
    with pytest.raises(GatewayDiscoveryError, match="不能包含凭据"):
        GatewayDiscovery.validate_base_url("http://user:pass@127.0.0.1:51921")


def test_default_home_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKBUDDY_HOME", raising=False)
    home = GatewayDiscovery._default_home()
    assert home == Path.home() / ".workbuddy"


def test_default_home_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKBUDDY_HOME", "/custom/workbuddy")
    home = GatewayDiscovery._default_home()
    assert home == Path("/custom/workbuddy")