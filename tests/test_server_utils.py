"""测试 server.py 中的安全边界、权限解析和 JSON 编码工具函数。"""

from __future__ import annotations

import json

import pytest

from workbuddy_acp_bridge.server import (
    _apply_permission_boundary,
    _json_content,
    _permission_mode,
    _thought_level,
)


class TestPermissionBoundary:
    def test_deny_mode_adds_readonly_boundary(self) -> None:
        result = _apply_permission_boundary("总结文档", "deny")
        assert "只读" in result
        assert "总结文档" in result
        assert result.startswith("[桥接器安全边界]")

    def test_allow_once_mode_adds_single_op_boundary(self) -> None:
        result = _apply_permission_boundary("发送消息", "allow_once")
        assert "单次" in result
        assert "发送消息" in result
        assert result.startswith("[桥接器安全边界]")


class TestPermissionMode:
    def test_accepts_deny(self) -> None:
        assert _permission_mode("deny") == "deny"

    def test_accepts_allow_once(self) -> None:
        assert _permission_mode("allow_once") == "allow_once"

    def test_rejects_allow_always(self) -> None:
        with pytest.raises(ValueError, match="permission_mode"):
            _permission_mode("allow_always")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(ValueError):
            _permission_mode(123)


class TestThoughtLevel:
    def test_accepts_disabled(self) -> None:
        assert _thought_level("disabled") == "disabled"

    def test_accepts_all_valid_values(self) -> None:
        for level in ("disabled", "minimal", "low", "medium", "high", "xhigh", "max", "enabled"):
            assert _thought_level(level) == level

    def test_rejects_invalid(self) -> None:
        with pytest.raises(ValueError):
            _thought_level("extreme")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(ValueError):
            _thought_level(True)


class TestJsonContent:
    def test_encodes_utf8_safe(self) -> None:
        content = _json_content({"ok": True, "msg": "中文测试"})
        assert content.type == "text"
        parsed = json.loads(content.text)
        assert parsed["ok"] is True
        assert parsed["msg"] == "中文测试"

    def test_ensure_ascii_false(self) -> None:
        """验证 ensure_ascii=False 使中文可读。"""
        content = _json_content({"msg": "你好"})
        assert "\\u" not in content.text