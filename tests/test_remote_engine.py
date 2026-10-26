"""远程引擎适配器测试：用本地假 worker，验证协议与错误处理（不依赖 CosyVoice）。"""

from __future__ import annotations

import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest

from voxd.engine.base import EngineError
from voxd.engine.remote import RemoteEngine
from voxd.voices import Voice


def make_wav(seconds=0.2, sr=24000):
    n = int(sr * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.zeros(n, dtype="<i2")).tobytes())
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    seen: list[dict] = []
    fail = False

    def log_message(self, *a):  # 静音
        pass

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"ok": True, "loaded": True, "sr": 24000}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        Handler.seen.append(json.loads(raw or b"{}"))
        if Handler.fail:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"detail":"boom"}')
            return
        data = make_wav()
        self.send_response(200)
        self.send_header("content-type", "audio/wav")
        self.send_header("x-sr", "24000")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def worker():
    Handler.seen.clear()
    Handler.fail = False
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_remote_engine_roundtrip(worker):
    eng = RemoteEngine(worker)
    eng.load()
    assert eng.caps.instruct and eng.caps.clone

    voice = Voice(id="coco", ref_audio="prompt.wav")
    chunks = list(eng.render("你好呀", voice=voice, instruct="用轻快的语气"))
    y = np.concatenate(chunks)
    assert len(y) == 4800  # 0.2s @ 24k

    sent = Handler.seen[-1]
    assert sent["text"] == "你好呀"
    assert sent["instruct"].endswith("<|endofprompt|>"), "instruct 必须带结束 token，否则会被念出来"
    assert sent["prompt"] == "prompt.wav"


def test_remote_engine_errors(worker):
    eng = RemoteEngine(worker)
    eng.load()
    Handler.fail = True
    with pytest.raises(EngineError):
        list(eng.render("你好", voice=Voice(id="coco", ref_audio="p.wav")))


def test_imperative_mapping():
    """命令式 instruct：CosyVoice 这类模型要的是「用…的语气说这句话」，不是描述。"""
    from voxd.engine.base import tags_to_imperative

    s = tags_to_imperative(["不耐烦"], 0.9)
    assert s.startswith("用不耐烦") and "更强烈" in s
    calm = tags_to_imperative(["温柔"], 0.2)
    assert calm.startswith("用温柔") and "克制" in calm
    assert tags_to_imperative(["不存在的标签"]) is None
