"""隔离配置验证 DPAPI 的迁移、保存失败及旧版回退。"""
import os

import pytest
import yaml

from app.core import config


pytestmark = pytest.mark.skipif(os.name != "nt", reason="需要 Windows DPAPI")


def test_legacy_credentials_migrate_and_roundtrip(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text(
        "transcriber:\n  cloud:\n    api_key: fake-asr-迁移\n"
        "translator:\n  llm:\n    vendor: custom\n    api_key: fake-llm-迁移\n"
        "user_custom:\n  retain: true\n", encoding="utf-8")
    loaded = config.load_config(target, migrate_credentials=True)
    assert loaded.get("transcriber.cloud.api_key") == "fake-asr-迁移"
    assert loaded.get("translator.llm.api_key") == "fake-llm-迁移"
    raw = target.read_text(encoding="utf-8")
    assert "fake-" not in raw
    assert raw.count("api_key_dpapi:") == 2
    assert config.load_config(target).get("translator.llm.api_key") == "fake-llm-迁移"
    assert config.load_config(target, migrate_credentials=True).get("user_custom.retain") is True
    assert target.read_text(encoding="utf-8") == raw

    config.restore_legacy_credentials(target)
    downgraded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert downgraded["transcriber"]["cloud"] == {"api_key": "fake-asr-迁移"}
    assert downgraded["translator"]["llm"]["api_key"] == "fake-llm-迁移"
    assert downgraded["user_custom"] == {"retain": True}


def test_save_rejects_failed_encryption_without_overwriting_file(tmp_path, monkeypatch):
    target = tmp_path / "config.yaml"
    original = "translator:\n  llm:\n    vendor: custom\n    api_key: fake-previous\n"
    target.write_text(original, encoding="utf-8")
    from app.core import windows_data
    def fail(_):
        raise OSError("模拟 DPAPI 故障")
    monkeypatch.setattr(windows_data, "protect", fail)
    loaded = config.load_config(target, migrate_credentials=True)
    assert loaded.get("translator.llm.api_key") == "fake-previous"
    with pytest.raises(OSError):
        config.save_config({"translator": {"llm": {"api_key": "fake-replacement"}}}, target)
    assert target.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*.tmp"))
