"""config 单元测试：默认值、深合并、点路径读写。"""
from app.core.config import Config, deep_merge


def test_deep_merge_nested():
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    deep_merge(base, {"a": {"b": 10}, "e": 4})
    assert base == {"a": {"b": 10, "c": 2}, "d": 3, "e": 4}


def test_config_dotted_get():
    cfg = Config({"download": {"aria2c": {"split": 16}}})
    assert cfg.get("download.aria2c.split") == 16
    assert cfg.get("download.missing.key", "fallback") == "fallback"


def test_config_set():
    cfg = Config({"transcriber": {"model": "small"}})
    cfg.set("transcriber.model", "large-v3")
    assert cfg.get("transcriber.model") == "large-v3"


def test_defaults_load():
    from app.core.config import load_config  # 不依赖 config.yaml 存在
    cfg = load_config("config.example.yaml")  # 样例文件应当可解析
    assert cfg.get("engines.order")[0] == "ytdlp"
