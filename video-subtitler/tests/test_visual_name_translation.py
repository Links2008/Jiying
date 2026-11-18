"""画面人名的提交意图、批量翻译和可恢复缓存。"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api import server
from app.core.config import Config
from app.services.translator import LLMTranslator


def test_visual_translation_keeps_audio_disabled_and_legacy_submit_unchanged(monkeypatch):
    app = server.create_app()
    runtime = app.state.runtime
    monkeypatch.setattr(runtime.router, "probe", lambda *_: ("local", SimpleNamespace(title="clip")))
    monkeypatch.setattr(runtime, "start_task", lambda *_: None)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as api:
        body = {"url": "https://example.test/clip", "langs": ["zh"], "person_notes": True, "translate": True}
        legacy = api.post("/api/v1/tasks", json=body)
        visual = api.post("/api/v1/tasks", json={**body, "audio_subtitles": False})
        assert legacy.status_code == visual.status_code == 200
        assert legacy.json()["audio_subtitles"] == 1
        assert visual.json()["audio_subtitles"] == 0
        assert visual.json()["target_langs"] == "zh" and visual.json()["translate"] == 1
        assert visual.json()["id"] != legacy.json()["id"]


def test_screen_people_batch_translates_both_fields_filters_title_and_reuses_response(tmp_path, monkeypatch):
    provider = LLMTranslator(Config({"translator": {"provider": "llm", "llm": {"api_key": "isolated"}}}))
    people = [{"source": "Lucy Morgan", "target": "Lucy Morgan", "sense": "Research Director", "start": 0},
              {"source": "Blue River", "target": "Blue River", "sense": "", "start": 2}]
    calls = []

    def reply(prompt):
        calls.append(prompt)
        return json.dumps({"people": [{"id": 1, "person": True, "name": "露西·摩根", "title": "研究总监"},
                                       {"id": 2, "person": False}]})

    monkeypatch.setattr(provider, "_call_api", reply)
    translate = lambda: provider.translate_person_notes(people, "zh", [], tmp_path, lambda: None)
    assert [(note["target"], note["sense"]) for note in translate()] == [("露西·摩根", "研究总监")]
    assert translate() == translate()
    assert len(calls) == 1 and "Blue River" in calls[0]
    assert people[0]["sense"] == "Research Director"


def test_screen_people_use_spoken_identity_and_title_map_offline(tmp_path, monkeypatch):
    provider = LLMTranslator(Config({}))
    monkeypatch.setattr(provider, "_call_api", lambda _: (_ for _ in ()).throw(AssertionError("unexpected LLM")))
    screen = {"source": "John F. Kennedy", "target": "John F. Kennedy", "sense": "Research Director", "start": 60}
    known = [{"source": "John F Kennedy", "target": "约翰·肯尼迪", "sense": "", "kind": "person", "start": 1}]
    translated = provider.translate_person_notes([screen], "zh", known, tmp_path, lambda: None)
    assert translated[0]["target"] == "约翰·肯尼迪" and translated[0]["sense"] == "研究总监"


def test_screen_translation_uses_explicit_keyless_loopback(tmp_path):
    requests_seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests_seen.append((self.path, self.headers.get("Authorization"), body["model"]))
            people = {"people": [{"id": 1, "person": True, "name": "露西·摩根", "title": "研究总监"}]}
            response = json.dumps({"choices": [{"message": {"content": json.dumps(people)}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as service:
        worker = threading.Thread(target=service.serve_forever)
        worker.start()
        try:
            provider = LLMTranslator(Config({"translator": {"provider": "llm", "llm": {
                "base_url": f"http://127.0.0.1:{service.server_port}/v1", "model": "local", "api_key": ""
            }}}))
            notes = [{"source": "Lucy Morgan", "target": "Lucy Morgan", "sense": "Research Director", "start": 0}]
            result = provider.translate_person_notes(notes, "zh", [], tmp_path, lambda: None)
            assert result[0]["target"] == "露西·摩根"
            assert result[0]["sense"] == "研究总监"
            assert requests_seen == [("/v1/chat/completions", None, "local")]
        finally:
            service.shutdown()
            worker.join(timeout=3)


def test_keyless_remote_visual_translation_stays_offline(tmp_path, monkeypatch):
    provider = LLMTranslator(Config({"translator": {"provider": "llm", "llm": {
        "base_url": "https://example.invalid/v1", "model": "remote"
    }}}))
    monkeypatch.setattr(provider, "_call_api", lambda _: (_ for _ in ()).throw(AssertionError("unexpected request")))
    notes = [{"source": "Lucy Morgan", "target": "Lucy Morgan", "sense": "Research Director"}]
    result = provider.translate_person_notes(notes, "zh", [], tmp_path, lambda: None)
    assert result[0]["target"] == "Lucy Morgan"
    assert result[0]["sense"] == "研究总监"
