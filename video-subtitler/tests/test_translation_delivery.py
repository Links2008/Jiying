"""翻译调用需要保持编号、专名和术语，并按错误类型处理。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from app.core.config import Config
from app.core.errors import AppError
from app.services.translator import LLMTranslator


def test_deepseek_alias_migrates_only_on_the_official_api():
    def translator(base, model):
        return LLMTranslator(Config({"translator": {"llm": {"base_url": base, "model": model}}}))

    assert translator("https://api.deepseek.com/v1", "deepseek-chat")._model() == "deepseek-flash"
    assert translator("https://api.deepseek.com/v1", "deepseek-v4-pro")._model() == "deepseek-v4-pro"
    assert translator("https://proxy.example.test/v1", "deepseek-chat")._model() == "deepseek-chat"


def test_bad_credentials_fail_after_one_request(monkeypatch):
    translator = LLMTranslator(Config({}))
    response = requests.Response()
    response.status_code = 401
    count = []

    def reject(prompt):
        count.append(prompt)
        raise requests.HTTPError("Unauthorized", response=response)

    monkeypatch.setattr(translator, "_call_api", reject)
    monkeypatch.setattr("app.services.translator.time.sleep", lambda _: None)
    with pytest.raises(AppError, match="认证"):
        translator.translate(["Please wait."], "en", "zh")
    assert len(count) == 1


def test_changed_model_code_and_glossary_are_repaired_together(monkeypatch):
    worker = LLMTranslator(Config({"translator": {"llm": {
        "glossary": {"zh": {"pressure relief valve": "泄压阀"}}}}}))
    responses = iter([
        {"translations": [{"id": 1, "text": "检查 S7-1500 的释压装置。"}]},
        {"translations": [{"id": 1, "text": "检查 S7-1200 的泄压阀。"}]},
    ])
    prompts = []

    def request(prompt):
        prompts.append(prompt)
        return json.dumps(next(responses))

    monkeypatch.setattr(worker, "_call_api", request)
    assert worker.translate(["Check the S7-1200 pressure relief valve."], "en", "zh") == [
        "检查 S7-1200 的泄压阀。"]
    assert "S7-1200" in prompts[1] and "泄压阀" in prompts[1]
    assert "重新" in prompts[1]


def test_supported_model_uses_the_same_connection_for_two_batches():
    ports = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            index = int(body["messages"][-1]["content"].splitlines()[-1].split(".")[0])
            ports.append(self.client_address[1])
            text = json.dumps({"translations": [{"id": index, "text": "测试"}]})
            payload = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            worker = LLMTranslator(Config({"translator": {"llm": {"batch_size": 1,
                "model": "fake", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}}}))
            assert worker.translate(["one", "two"], "en", "zh") == ["测试", "测试"]
            assert len(ports) == 2 and len(set(ports)) == 1
        finally:
            server.shutdown()
