from copy import deepcopy

import pytest

from rimagent.config import _DEFAULTS, load


def test_nested_local_override_preserves_other_options_and_utf8(tmp_path):
    root = tmp_path / "日本語の設定"
    root.mkdir()
    path = root / "config.yaml"
    path.write_text('llm:\n  model: Qwen/日本語\nsteward:\n  orders:\n    "off": [combat]\n', encoding="utf-8")
    path.with_name("config.local.yaml").write_text('steward:\n  orders:\n    enabled: false\n', encoding="utf-8-sig")
    cfg = load(path)
    assert cfg["llm"]["model"] == "Qwen/日本語"
    assert cfg["llm"]["provider"] == "qwen"
    assert cfg["steward"]["orders"]["off"] == ["combat"]
    assert cfg["steward"]["orders"]["enabled"] is False


def test_loaded_mutable_defaults_are_isolated(tmp_path):
    before = deepcopy(_DEFAULTS)
    cfg = load(tmp_path / "absent.yaml")
    cfg["steward"]["orders"]["off"].append("combat")
    cfg["play"]["seeds"].append("new")
    assert _DEFAULTS == before
    assert load(tmp_path / "absent.yaml")["steward"]["orders"]["off"] == []


def test_invalid_yaml_root_is_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text('- llm\n- provider\n', encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load(path)


def test_explicit_empty_superseded_map_replaces_base_mapping(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text('steward:\n  orders:\n    superseded_watchers: {combat: combat}\n', encoding="utf-8")
    path.with_name("config.local.yaml").write_text('steward:\n  orders:\n    superseded_watchers: {}\n', encoding="utf-8")
    assert load(path)["steward"]["orders"]["superseded_watchers"] == {}
