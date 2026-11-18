"""验证打包运行时与平台错误分类，避免把网络问题变成登录步骤。"""
from app.core.config import Config
from app.core.errors import Codes
from app.services.engines import ytdlp_engine


def test_bundled_javascript_runtime_works_without_system_install(monkeypatch, tmp_path):
    runtime = tmp_path / "deno.exe"
    runtime.write_bytes(b"isolated-runtime-placeholder")
    monkeypatch.setattr(ytdlp_engine, "bin_dir", lambda: tmp_path)
    monkeypatch.setattr(ytdlp_engine.shutil, "which", lambda _: None)
    options = ytdlp_engine.YtDlpEngine(Config({}))._common_opts()
    assert options["js_runtimes"]["deno"]["path"] == str(runtime)
    assert not options.get("remote_components")


def test_system_node_is_a_fallback_for_source_development(monkeypatch, tmp_path):
    monkeypatch.setattr(ytdlp_engine, "bin_dir", lambda: tmp_path)
    monkeypatch.setattr(ytdlp_engine.shutil, "which", lambda name: "node-path" if name == "node" else None)
    assert ytdlp_engine.YtDlpEngine(Config({}))._common_opts()["js_runtimes"]["node"]["path"] == "node-path"


def test_tls_compatibility_error_does_not_ask_for_login():
    error = ytdlp_engine._ytdlp_error(
        'This request has been blocked due to its TLS fingerprint. Install an impersonation dependency; security/cookies',
        '链接解析', 'https://vimeo.com/54469442')
    assert error.code == Codes.NET_FAILED
    assert '登录' not in error.message
    assert 'security/cookies' not in error.message


def test_authenticated_content_still_requests_login():
    error = ytdlp_engine._ytdlp_error('Login required to watch this video', '链接解析',
                                    'https://www.douyin.com/video/123')
    assert error.code == Codes.AUTH_NEED_COOKIES


def test_parser_failure_keeps_technical_details_out_of_user_message():
    error = ytdlp_engine._ytdlp_error(
        'Unable to extract initial state; please report this issue on https://github.com/yt-dlp/yt-dlp/issues',
        '链接解析', 'https://www.xiaohongshu.com/explore/123')
    assert 'github.com' not in error.message
    assert 'initial state' in error.detail
