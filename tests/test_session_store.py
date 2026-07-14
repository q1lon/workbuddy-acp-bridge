"""测试 _PersistentSessionStore 的跨平台文件锁定和读写。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from workbuddy_acp_bridge.server import _PersistentSessionStore


def test_read_returns_none_when_file_missing(tmp_path: Path) -> None:
    store = _PersistentSessionStore(path=tmp_path / "state.json")
    assert store.read() is None


def test_write_and_read_roundtrip(tmp_path: Path) -> None:
    store = _PersistentSessionStore(path=tmp_path / "state.json")
    store.write("session-abc-123")
    assert store.read() == "session-abc-123"


def test_write_creates_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "state.json"
    store = _PersistentSessionStore(path=nested)
    store.write("session-abc")
    assert nested.is_file()
    assert store.read() == "session-abc"


def test_write_atomicity(tmp_path: Path) -> None:
    """验证写入是原子的：临时文件不会出现在最终路径。"""
    store = _PersistentSessionStore(path=tmp_path / "state.json")
    store.write("session-abc")
    # 目录中不应有 .tmp 残留
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert len(tmp_files) == 0


def test_read_raises_on_invalid_json(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text("not json", encoding="utf-8")
    store = _PersistentSessionStore(path=state_file)
    with pytest.raises(RuntimeError, match="持久会话状态文件"):
        store.read()


def test_read_raises_on_missing_session_id(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
    store = _PersistentSessionStore(path=state_file)
    with pytest.raises(RuntimeError, match="无效 session_id"):
        store.read()


def test_clear_deletes_when_matches(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    store = _PersistentSessionStore(path=state_file)
    store.write("session-abc")
    store.clear("session-abc")
    assert not state_file.exists()


def test_clear_does_not_delete_when_mismatch(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    store = _PersistentSessionStore(path=state_file)
    store.write("session-abc")
    store.clear("session-xyz")  # 不同 ID
    assert state_file.exists()
    assert store.read() == "session-abc"


def test_clear_noop_when_file_missing(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    store = _PersistentSessionStore(path=state_file)
    store.clear("session-abc")  # 不应抛出异常
    assert not state_file.exists()