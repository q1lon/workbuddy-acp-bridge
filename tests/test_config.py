"""测试 _load_default_model_id 的配置读取逻辑。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workbuddy_acp_bridge.server import _load_default_model_id
from workbuddy_acp_bridge.acp_client import DEFAULT_MODEL_ID


def test_returns_default_when_file_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "nonexistent.json"
    result = _load_default_model_id(config_path)
    assert result == DEFAULT_MODEL_ID


def test_returns_default_when_model_id_null(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"model_id": None}), encoding="utf-8")
    result = _load_default_model_id(config_path)
    assert result == DEFAULT_MODEL_ID


def test_returns_default_when_field_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({}), encoding="utf-8")
    result = _load_default_model_id(config_path)
    assert result == DEFAULT_MODEL_ID


def test_returns_default_when_empty_string(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"model_id": ""}), encoding="utf-8")
    result = _load_default_model_id(config_path)
    assert result == DEFAULT_MODEL_ID


def test_reads_configured_model_id(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"model_id": "custom-local:kimi-k2.6"}), encoding="utf-8")
    result = _load_default_model_id(config_path)
    assert result == "custom-local:kimi-k2.6"


def test_strips_whitespace(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"model_id": "  auto  "}), encoding="utf-8")
    result = _load_default_model_id(config_path)
    assert result == "auto"


def test_rejects_invalid_model_id(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"model_id": "invalid model!"}), encoding="utf-8")
    with pytest.raises(ValueError, match="model_id 格式无效"):
        _load_default_model_id(config_path)


def test_rejects_oversized_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("x" * (64 * 1024 + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="不能超过"):
        _load_default_model_id(config_path)


def test_rejects_non_dict_root(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="必须是 JSON 对象"):
        _load_default_model_id(config_path)


def test_rejects_non_string_model_id(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"model_id": 123}), encoding="utf-8")
    with pytest.raises(ValueError, match="必须是字符串"):
        _load_default_model_id(config_path)