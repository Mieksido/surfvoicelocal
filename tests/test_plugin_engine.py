"""插件引擎测试：任何外部 TTS 只要遵守 HTTP 契约就能插进来。

这里用一个本地 http.server 假装是"别人的 TTS 服务"，验证：
1. config.toml 的 [engines.<name>] 能生成引擎
2. 能力声明（caps）被正确读取，路由才敢用
3. render() 能拿回 wav 并转成 float32
4. 返回裸 PCM + X-Sample-Rate 也认
"""

from __future__ import annotations

import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np
import pytest

from voxd.api import Config, _specs
from voxd.engine.http_plugin import HttpEngine
from voxd.voices import Voice


def _wav_bytes(sr: int = 24000, samples: int = 2400) -> bytes:
    t = np.linspace(0, 0.1, samples, endpoint=False)
    pcm = (np.sin(2 * np.pi * 220 * t) * 0.3 * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class _Handler(BaseHTTPRequestHandler):
    mode = "wav"

    def log_message(self, *a):  # noqa: D401  静音
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        payload = json.loads(body)
        text = payload["text"]
        if self.mode == "wav":
            data = _wav_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:  # 裸 PCM + 采样率头
            pcm = np.zeros(2400, dtype="<i2").tobytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-Sample-Rate", "24000")
            self.send_header("X-Echo-Len", str(len(text)))
            self.send_header("Content-Length", str(len(pcm)))
            self.end_headers()
            self.wfile.write(pcm)


@pytest.fixture()
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield httpd
    httpd.shutdown()


def test_config_can_declare_an_http_engine(tmp_path: Path, server):
    port = server.server_address[1]
    cfg_text = f"""
[engine]
primary = "mock"
fallbacks = []

[engines.mock]
kind = "http"
url = "http://127.0.0.1:{port}"
clone = true
instruct = false
langs = ["zh", "en"]
max_chars = 321
sr = 24000
"""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(cfg_text, "utf-8")

    cfg = Config(cfg_file)
    specs = _specs(cfg)
    assert "mock" in specs, "配置里的 [engines.mock] 应该出现在引擎表里"

    eng = specs["mock"]()
    assert isinstance(eng, HttpEngine)
    assert eng.caps.clone is True
    assert eng.caps.instruct is False
    assert eng.caps.langs == ("zh", "en")
    assert eng.caps.max_chars == 321


def test_http_engine_renders_wav(server):
    port = server.server_address[1]
    eng = HttpEngine(f"http://127.0.0.1:{port}", name="mock")
    eng.load()  # /health 存在，应该通过

    voice = Voice(id="v1", ref_audio="/tmp/x.wav", ref_text="hi")
    chunks = list(eng.render("你好", voice=voice, lang="zh"))
    assert len(chunks) == 1
    y = chunks[0]
    assert y.dtype == np.float32
    assert 0 < len(y) <= 2400
    assert np.abs(y).max() > 0.05, "应该是对应波形，不是静音"


def test_http_engine_accepts_raw_pcm_with_header(server, monkeypatch):
    _Handler.mode = "raw"
    try:
        port = server.server_address[1]
        eng = HttpEngine(f"http://127.0.0.1:{port}", name="mock")
        chunks = list(eng.render("你好", voice=Voice(id="v1"), lang="zh"))
        assert len(chunks[0]) == 2400
    finally:
        _Handler.mode = "wav"
