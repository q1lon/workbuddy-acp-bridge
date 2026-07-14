"""测试 WorkBuddyAcpClient 的纯校验方法（不涉及网络）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from workbuddy_acp_bridge.acp_client import MAX_PROMPT_CHARS, MODEL_ID_PATTERN, WorkBuddyAcpClient


# --- 权限模式 ---

def test_validate_permission_mode_accepts_deny() -> None:
    result = WorkBuddyAcpClient._validate_permission_mode("deny")
    assert result == "deny"


def test_validate_permission_mode_accepts_allow_once() -> None:
    result = WorkBuddyAcpClient._validate_permission_mode("allow_once")
    assert result == "allow_once"


def test_validate_permission_mode_rejects_other() -> None:
    with pytest.raises(ValueError, match="permission_mode"):
        WorkBuddyAcpClient._validate_permission_mode("allow_always")


# --- 提示词 ---

def test_validate_prompt_strips_whitespace() -> None:
    result = WorkBuddyAcpClient._validate_prompt("  hello  ")
    assert result == "hello"


def test_validate_prompt_rejects_empty() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        WorkBuddyAcpClient._validate_prompt("   ")


def test_validate_prompt_rejects_oversized() -> None:
    with pytest.raises(ValueError, match=f"不能超过 {MAX_PROMPT_CHARS}"):
        WorkBuddyAcpClient._validate_prompt("x" * (MAX_PROMPT_CHARS + 1))


# --- 会话 ID ---

def test_validate_session_id_strips() -> None:
    result = WorkBuddyAcpClient._validate_session_id("  abc  ")
    assert result == "abc"


def test_validate_session_id_rejects_empty() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        WorkBuddyAcpClient._validate_session_id("")


def test_validate_session_id_rejects_oversized() -> None:
    with pytest.raises(ValueError, match="过长"):
        WorkBuddyAcpClient._validate_session_id("x" * 257)


# --- 模型 ID ---

def test_validate_model_id_accepts_valid() -> None:
    result = WorkBuddyAcpClient._validate_model_id("custom-local:kimi-k2.6")
    assert result == "custom-local:kimi-k2.6"


def test_validate_model_id_accepts_auto() -> None:
    result = WorkBuddyAcpClient._validate_model_id("auto")
    assert result == "auto"


def test_validate_model_id_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="model_id 格式无效"):
        WorkBuddyAcpClient._validate_model_id("invalid model!")


# --- 超时 ---

def test_validate_timeout_accepts_valid() -> None:
    result = WorkBuddyAcpClient._validate_timeout(300)
    assert result == 300.0


def test_validate_timeout_rejects_too_short() -> None:
    with pytest.raises(ValueError, match="必须在 1 到 1800"):
        WorkBuddyAcpClient._validate_timeout(0)


def test_validate_timeout_rejects_too_long() -> None:
    with pytest.raises(ValueError, match="必须在 1 到 1800"):
        WorkBuddyAcpClient._validate_timeout(1801)


# --- 工作目录 ---

def test_validate_cwd_accepts_existing_dir(tmp_path: Path) -> None:
    result = WorkBuddyAcpClient._validate_cwd(tmp_path)
    assert result == str(tmp_path.resolve())


def test_validate_cwd_rejects_missing_dir(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent"
    with pytest.raises((ValueError, FileNotFoundError)):
        WorkBuddyAcpClient._validate_cwd(missing)


# --- 思考强度 ---

def test_validate_thought_level_accepts_disabled() -> None:
    result = WorkBuddyAcpClient._validate_thought_level("disabled")
    assert result == "disabled"


def test_validate_thought_level_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="thought_level 值无效"):
        WorkBuddyAcpClient._validate_thought_level("extreme")


# --- MODEL_ID_PATTERN ---

def test_model_id_pattern_allows_valid() -> None:
    assert MODEL_ID_PATTERN.fullmatch("auto")
    assert MODEL_ID_PATTERN.fullmatch("custom-local:kimi-k2.6")
    assert MODEL_ID_PATTERN.fullmatch("gpt-4o")
    assert MODEL_ID_PATTERN.fullmatch("claude-sonnet-4-6-20250514")


def test_model_id_pattern_rejects_invalid() -> None:
    assert not MODEL_ID_PATTERN.fullmatch("")
    assert not MODEL_ID_PATTERN.fullmatch("invalid model!")
    assert not MODEL_ID_PATTERN.fullmatch("x" * 129)
    assert not MODEL_ID_PATTERN.fullmatch("中文模型")